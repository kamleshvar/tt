from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func

from app.models import (
    MockTest,
    OPEN_TASK_STATUSES,
    RevisionRecord,
    StudyTask,
    TaskEvent,
    utcnow,
)
from app.planner.scheduler import available_minutes
from extensions import db

STAGE_ORDER = [
    "Not started",
    "Learning in progress",
    "Learning completed",
    "Practice in progress",
    "Practice completed",
    "Revision due",
    "Revision in progress",
    "Revision completed",
]


def refresh_overdue_tasks(user_id: int) -> int:
    today = date.today()
    stale = (
        StudyTask.query.filter(
            StudyTask.user_id == user_id,
            StudyTask.status.in_(["pending", "in_progress", "postponed"]),
            StudyTask.scheduled_date < today,
        )
        .all()
    )
    if not stale:
        return 0
    for task in stale:
        previous = task.status
        task.status = "overdue"
        db.session.add(
            TaskEvent(
                task=task,
                user_id=user_id,
                event_type="marked_overdue",
                from_status=previous,
                to_status="overdue",
                old_date=task.scheduled_date,
                new_date=task.scheduled_date,
                note="Automatically marked overdue after its scheduled date passed.",
            )
        )
    db.session.commit()
    return len(stale)


def _percent(done: int | float, total: int | float) -> float:
    if not total:
        return 0.0
    return round((done / total) * 100, 1)


def task_stats(user_id: int, start: date | None = None, end: date | None = None) -> dict:
    query = StudyTask.query.filter(
        StudyTask.user_id == user_id, StudyTask.status != "cancelled"
    )
    if start:
        query = query.filter(StudyTask.scheduled_date >= start)
    if end:
        query = query.filter(StudyTask.scheduled_date <= end)
    tasks = query.all()
    total = len(tasks)
    completed = sum(1 for task in tasks if task.status == "completed")
    open_count = sum(1 for task in tasks if task.status in OPEN_TASK_STATUSES)
    overdue = sum(
        1
        for task in tasks
        if task.status == "overdue"
        or (
            task.status in OPEN_TASK_STATUSES
            and task.scheduled_date < date.today()
        )
    )
    planned_minutes = sum(task.estimated_minutes for task in tasks)
    completed_minutes = sum(
        (task.actual_minutes or task.estimated_minutes) for task in tasks if task.status == "completed"
    )
    return {
        "total": total,
        "completed": completed,
        "open": open_count,
        "overdue": overdue,
        "completion_percent": _percent(completed, total),
        "planned_minutes": planned_minutes,
        "completed_minutes": completed_minutes,
        "in_progress": sum(1 for task in tasks if task.status == "in_progress"),
        "pending": sum(
            1 for task in tasks if task.status in {"pending", "postponed", "overdue"}
        ),
    }


def _task_type_minutes(tasks: list[StudyTask], task_type: str) -> tuple[int, int]:
    relevant = [task for task in tasks if task.task_type == task_type]
    total = sum(task.estimated_minutes for task in relevant)
    done = sum(
        (task.actual_minutes or task.estimated_minutes)
        for task in relevant
        if task.status == "completed"
    )
    return done, total


def selected_chapters(user) -> list:
    chapters = []
    for link in user.subjects:
        if not link.is_active or not link.syllabus_subject or link.syllabus_subject.is_removed:
            continue
        subject = link.syllabus_subject
        for unit in subject.units:
            for chapter in unit.chapters:
                if not chapter.is_removed:
                    chapters.append(chapter)
    return chapters


def chapter_progress_rows(user) -> list[dict]:
    chapters = selected_chapters(user)
    tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == user.id, StudyTask.status != "cancelled"
        )
        .order_by(StudyTask.scheduled_date, StudyTask.start_time)
        .all()
    )
    records = {
        (record.syllabus_chapter_id, record.revision_number): record
        for record in RevisionRecord.query.filter_by(user_id=user.id).all()
    }
    by_chapter: dict[int, list[StudyTask]] = {}
    for task in tasks:
        if task.syllabus_chapter_id:
            by_chapter.setdefault(task.syllabus_chapter_id, []).append(task)

    rows = []
    for chapter in chapters:
        chapter_tasks = by_chapter.get(chapter.id, [])
        learning_done, learning_total = _task_type_minutes(chapter_tasks, "learning")
        practice_done, practice_total = _task_type_minutes(chapter_tasks, "practice")
        revision_done, revision_total = _task_type_minutes(chapter_tasks, "revision")
        revision_records = [
            record for key, record in records.items() if key[0] == chapter.id
        ]
        if revision_records:
            fallback_revision = chapter.revision_minutes or max(
                0, chapter.estimated_minutes // 5
            )
            revision_total = max(revision_total, fallback_revision)
            revision_done = max(
                revision_done,
                sum(
                    fallback_revision
                    for record in revision_records
                    if record.status == "completed"
                ),
            )

        stage = _chapter_stage(
            chapter_tasks, learning_done, learning_total, practice_done, practice_total,
            revision_done, revision_total,
        )
        next_task = next(
            (
                task
                for task in chapter_tasks
                if task.status in OPEN_TASK_STATUSES
            ),
            None,
        )
        rows.append(
            {
                "chapter": chapter,
                "subject": chapter.subject,
                "learning_done": learning_done,
                "learning_total": learning_total,
                "practice_done": practice_done,
                "practice_total": practice_total,
                "revision_done": revision_done,
                "revision_total": revision_total,
                "stage": stage,
                "tasks": chapter_tasks,
                "next_task": next_task,
                "task_completion": _percent(
                    sum(1 for task in chapter_tasks if task.status == "completed"),
                    len(chapter_tasks),
                ),
            }
        )
    return rows


def _chapter_stage(
    tasks, learning_done, learning_total, practice_done, practice_total,
    revision_done, revision_total,
) -> str:
    if not tasks:
        return "Not started"
    if learning_total and learning_done < learning_total:
        return "Learning in progress" if learning_done > 0 else "Not started"
    if practice_total and practice_done < practice_total:
        return "Practice in progress" if practice_done > 0 else "Learning completed"
    if revision_total and revision_done < revision_total:
        return "Revision in progress" if revision_done > 0 else "Revision due"
    if revision_total:
        return "Revision completed"
    if practice_total:
        return "Practice completed"
    return "Learning completed"


def progress_snapshot(user) -> dict:
    tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == user.id, StudyTask.status != "cancelled"
        ).all()
    )
    task_summary = task_stats(user.id)
    chapters = selected_chapters(user)
    chapter_ids = {chapter.id for chapter in chapters}
    covered_ids = {
        task.syllabus_chapter_id
        for task in tasks
        if task.status == "completed"
        and task.task_type == "learning"
        and task.syllabus_chapter_id in chapter_ids
    }
    learning_done, learning_total = _task_type_minutes(tasks, "learning")
    practice_done, practice_total = _task_type_minutes(tasks, "practice")
    revision_done, revision_total = _task_type_minutes(tasks, "revision")

    mock_query = MockTest.query.filter(
        MockTest.user_id == user.id, MockTest.status != "cancelled"
    )
    mock_total = mock_query.count()
    mock_done = mock_query.filter(MockTest.status == "completed").count()

    today = date.today()
    horizon = today + timedelta(days=6)
    free_minutes = available_minutes(user, today, horizon) if user.preferences else 0
    planned_query = StudyTask.query.filter(
        StudyTask.user_id == user.id,
        StudyTask.status != "cancelled",
        StudyTask.scheduled_date >= today,
        StudyTask.scheduled_date <= horizon,
    )
    planned_open = int(
        planned_query.filter(StudyTask.status.in_(OPEN_TASK_STATUSES))
        .with_entities(func.coalesce(func.sum(StudyTask.estimated_minutes), 0))
        .scalar()
        or 0
    )

    return {
        "tasks": task_summary,
        "task_completion": task_summary["completion_percent"],
        "syllabus_coverage": _percent(len(covered_ids), len(chapter_ids)),
        "chapters_covered": len(covered_ids),
        "chapters_total": len(chapter_ids),
        "learning_completion": _percent(learning_done, learning_total),
        "practice_completion": _percent(practice_done, practice_total),
        "revision_completion": _percent(revision_done, revision_total),
        "mock_completion": _percent(mock_done, mock_total),
        "mock_done": mock_done,
        "mock_total": mock_total,
        "free_minutes_next_7_days": free_minutes,
        "planned_open_minutes_next_7_days": planned_open,
        "learning_minutes": (learning_done, learning_total),
        "practice_minutes": (practice_done, practice_total),
        "revision_minutes": (revision_done, revision_total),
    }


def mark_chapter_revision_complete(user_id: int, chapter_id: int, revision_number: int) -> None:
    record = RevisionRecord.query.filter_by(
        user_id=user_id,
        syllabus_chapter_id=chapter_id,
        revision_number=revision_number,
    ).first()
    if record and record.status != "completed":
        record.status = "completed"
        record.completed_date = date.today()
        db.session.commit()


def weekly_summary(user_id: int, start: date) -> dict:
    end = start + timedelta(days=6)
    return task_stats(user_id, start, end)


def recent_activity(user_id: int, limit: int = 8) -> list:
    return (
        TaskEvent.query.filter_by(user_id=user_id)
        .order_by(TaskEvent.created_at.desc())
        .limit(limit)
        .all()
    )
