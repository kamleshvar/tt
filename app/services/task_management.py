from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from app.models import (
    MockTest,
    OPEN_TASK_STATUSES,
    RevisionRecord,
    StudentActivityLog,
    StudySession,
    StudyTask,
    TaskEvent,
    utcnow,
)
from app.planner.scheduler import find_slot, minutes_to_time, time_to_minutes
from extensions import db

AUTO_SESSION_NOTE = "Auto-recorded from task completion"


class TaskTransitionError(Exception):
    pass


def _exam_deadline(user) -> date | None:
    return user.profile.exam_date if user.profile else None


def _event(task, user_id, event_type, from_status=None, old_date=None, note=None):
    db.session.add(
        TaskEvent(
            task=task,
            user_id=user_id,
            event_type=event_type,
            from_status=from_status,
            to_status=task.status,
            old_date=old_date,
            new_date=task.scheduled_date,
            note=note,
        )
    )


def set_task_status(user, task, target: str) -> bool:
    if task.user_id != user.id:
        raise TaskTransitionError("Task not found.")
    if target not in {"completed", "reopen", "in_progress"}:
        raise TaskTransitionError("Unsupported task transition.")
    previous = task.status
    if task.status == "cancelled":
        raise TaskTransitionError("Cancelled tasks cannot be changed.")

    if target == "completed":
        if task.status == "completed":
            return False
        if task.status not in OPEN_TASK_STATUSES:
            raise TaskTransitionError("This task cannot be completed from its current state.")
        now = utcnow()
        task.status = "completed"
        task.completed_at = now
        task.actual_minutes = task.actual_minutes or task.estimated_minutes
        _event(task, user.id, "completed", previous, note="Task marked complete.")
        db.session.add(
            StudySession(
                user_id=user.id,
                task_id=task.id,
                started_at=now - timedelta(minutes=task.actual_minutes),
                ended_at=now,
                duration_minutes=task.actual_minutes,
                notes=AUTO_SESSION_NOTE,
            )
        )
        if task.task_type == "revision" and task.syllabus_chapter_id:
            cycle = max(1, task.revision_cycle or 1)
            record = RevisionRecord.query.filter_by(
                user_id=user.id,
                syllabus_chapter_id=task.syllabus_chapter_id,
                revision_number=cycle,
            ).first()
            if record is None:
                record = RevisionRecord(
                    user_id=user.id,
                    syllabus_chapter_id=task.syllabus_chapter_id,
                    revision_number=cycle,
                    scheduled_date=task.scheduled_date,
                    status="pending",
                )
                db.session.add(record)
            db.session.flush()
            cycle_tasks = StudyTask.query.filter(
                StudyTask.user_id == user.id,
                StudyTask.syllabus_chapter_id == task.syllabus_chapter_id,
                StudyTask.task_type == "revision",
                StudyTask.revision_cycle == cycle,
                StudyTask.status != "cancelled",
            ).all()
            if cycle_tasks and all(item.status == "completed" for item in cycle_tasks):
                record.status = "completed"
                record.completed_date = date.today()
            else:
                record.status = "in_progress"
                record.completed_date = None
        if task.task_type == "mock":
            mock = MockTest.query.filter_by(user_id=user.id, task_id=task.id).first()
            if mock is not None:
                mock.status = "completed"
                mock.completed_date = date.today()
        db.session.add(
            StudentActivityLog(
                user_id=user.id,
                action="task_completed",
                entity_type="study_task",
                entity_id=task.id,
            )
        )
        db.session.commit()
        return True

    if target == "reopen":
        if task.status != "completed":
            return False
        task.status = "overdue" if task.scheduled_date < date.today() else "pending"
        task.completed_at = None
        task.actual_minutes = None
        _event(
            task, user.id, "reopened", previous,
            note="Completion was reversed by the student.",
        )
        StudySession.query.filter_by(
            user_id=user.id, task_id=task.id, notes=AUTO_SESSION_NOTE
        ).delete(synchronize_session=False)
        if task.task_type == "revision" and task.syllabus_chapter_id:
            cycle = max(1, task.revision_cycle or 1)
            record = RevisionRecord.query.filter_by(
                user_id=user.id,
                syllabus_chapter_id=task.syllabus_chapter_id,
                revision_number=cycle,
            ).first()
            if record is not None:
                record.status = "pending"
                record.completed_date = None
        if task.task_type == "mock":
            mock = MockTest.query.filter_by(user_id=user.id, task_id=task.id).first()
            if mock is not None:
                mock.status = "pending"
                mock.completed_date = None
        db.session.commit()
        return True

    if target == "in_progress":
        if task.status == "in_progress":
            return False
        if task.status not in OPEN_TASK_STATUSES:
            raise TaskTransitionError("Only open tasks can be started.")
        task.status = "in_progress"
        _event(task, user.id, "started", previous)
        db.session.commit()
        return True
    return False


def postpone_task(
    user,
    task: StudyTask,
    mode: str,
    requested_date: date | None = None,
) -> tuple[bool, str]:
    if task.user_id != user.id:
        raise TaskTransitionError("Task not found.")
    if task.status not in OPEN_TASK_STATUSES:
        raise TaskTransitionError("Only an incomplete task can be postponed.")
    today = date.today()
    exam = _exam_deadline(user)
    if mode == "tomorrow":
        start = today + timedelta(days=1)
        end = start
    elif mode == "date":
        if requested_date is None:
            raise TaskTransitionError("Choose a future date.")
        if requested_date <= today:
            raise TaskTransitionError("Choose a date after today.")
        start = requested_date
        end = requested_date
    elif mode == "next":
        start = today + timedelta(days=1)
        end = exam or (today + timedelta(days=60))
    else:
        raise TaskTransitionError("Choose a valid postponement option.")
    if exam and end > exam:
        end = exam
    if exam and start > exam:
        raise TaskTransitionError("The board examination date leaves no future study slot.")
    slot = find_slot(
        user,
        task.estimated_minutes,
        start,
        end,
        exclude_task_id=task.id,
    )
    if slot is None:
        return False, (
            "No conflict-free slot was found for that date. Try “next suitable slot” "
            "or shorten the task with Split."
        )
    old_date = task.scheduled_date
    old_time = task.time_label
    previous_status = task.status
    task.scheduled_date = slot.day
    task.start_time = slot.start_time
    task.end_time = slot.end_time
    task.status = "postponed"
    _event(
        task,
        user.id,
        "postponed",
        previous_status,
        old_date=old_date,
        note=f"Moved from {old_date.isoformat()} ({old_time}) to {slot.day.isoformat()} "
        f"{slot.start_time.strftime('%H:%M')}–{slot.end_time.strftime('%H:%M')}.",
    )
    db.session.commit()
    return True, f"Task moved to {slot.day.strftime('%a, %d %b')} at {slot.start_time.strftime('%H:%M')}."


def split_task(user, task: StudyTask, parts: int = 2) -> tuple[bool, str]:
    if task.user_id != user.id:
        raise TaskTransitionError("Task not found.")
    if task.status not in OPEN_TASK_STATUSES:
        raise TaskTransitionError("Only an incomplete task can be split.")
    parts = max(2, min(4, int(parts)))
    if task.estimated_minutes < parts * 15:
        raise TaskTransitionError("This task is too short to split further.")
    base_title = re.sub(r"\s+\(part \d+/\d+\)$", "", task.title).strip()
    base_duration = task.estimated_minutes // parts
    durations = [base_duration] * parts
    durations[-1] += task.estimated_minutes - sum(durations)
    old_date = task.scheduled_date
    original_window = task.estimated_minutes
    created = []
    try:
        task.title = f"{base_title} (part 1/{parts})"
        task.estimated_minutes = durations[0]
        if task.start_time and time_to_minutes(task.end_time) - time_to_minutes(task.start_time) >= durations[0]:
            task.end_time = minutes_to_time(time_to_minutes(task.start_time) + durations[0])
        else:
            slot = find_slot(
                user, durations[0], date.today(), _exam_deadline(user) or date.today() + timedelta(days=60),
                exclude_task_id=task.id,
            )
            if slot is None:
                raise TaskTransitionError("No free slot is available for the split parts.")
            task.scheduled_date = slot.day
            task.start_time = slot.start_time
            task.end_time = slot.end_time
        task.status = "pending"
        task.original_scheduled_date = task.original_scheduled_date or old_date
        db.session.flush()
        created.append(task)
        for index in range(1, parts):
            slot = find_slot(
                user, durations[index], date.today(),
                _exam_deadline(user) or date.today() + timedelta(days=60),
            )
            if slot is None:
                raise TaskTransitionError(
                    "There is not enough conflict-free time to place every split part. "
                    "No changes were made."
                )
            part = StudyTask(
                user_id=user.id,
                plan_id=task.plan_id,
                syllabus_chapter_id=task.syllabus_chapter_id,
                syllabus_topic_id=task.syllabus_topic_id,
                parent_task_id=task.id,
                task_type=task.task_type,
                title=f"{base_title} (part {index + 1}/{parts})",
                original_scheduled_date=old_date,
                scheduled_date=slot.day,
                start_time=slot.start_time,
                end_time=slot.end_time,
                estimated_minutes=durations[index],
                status="pending",
                priority=task.priority,
                revision_cycle=task.revision_cycle,
                notes=f"Split from a {original_window}-minute task.",
            )
            db.session.add(part)
            db.session.flush()
            created.append(part)
        _event(
            task, user.id, "split", "pending", old_date,
            note=f"Task split into {parts} parts.",
        )
        db.session.commit()
        return True, f"Task split into {parts} independently scheduled parts."
    except Exception:
        db.session.rollback()
        raise
