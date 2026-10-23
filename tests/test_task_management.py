from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app.models import (
    RevisionRecord,
    StudyPlan,
    StudySession,
    StudyTask,
    SyllabusVersion,
    TaskEvent,
    User,
)
from app.planner.scheduler import minutes_to_time, time_to_minutes
from app.services.task_management import (
    TaskTransitionError,
    postpone_task,
    set_task_status,
    split_task,
)
from extensions import db


def _new_task(user, *, day=None, start=time(17, 0), minutes=45, task_type="learning"):
    version = SyllabusVersion.query.filter_by(is_current=True).first()
    plan = StudyPlan(
        user_id=user.id,
        syllabus_version_id=version.id,
        version_number=99,
        status="active",
        horizon_start=date.today(),
        horizon_end=user.profile.exam_date or date.today() + timedelta(days=60),
    )
    db.session.add(plan)
    db.session.flush()
    task = StudyTask(
        user_id=user.id,
        plan_id=plan.id,
        task_type=task_type,
        title="Test task",
        original_scheduled_date=day or date.today(),
        scheduled_date=day or date.today(),
        start_time=start,
        end_time=minutes_to_time(start.hour * 60 + start.minute + minutes),
        estimated_minutes=minutes,
        status="pending",
        priority=10,
        revision_cycle=1 if task_type == "revision" else 0,
    )
    db.session.add(task)
    db.session.commit()
    return task


def test_complete_and_reopen_updates_history_and_sessions(app, student_factory):
    user_id = student_factory()
    with app.app_context():
        user = db.session.get(User, user_id)
        task = _new_task(user)

        assert set_task_status(user, task, "completed") is True
        assert task.status == "completed"
        assert task.completed_at is not None
        assert task.actual_minutes == 45
        assert TaskEvent.query.filter_by(task_id=task.id, event_type="completed").count() == 1
        assert StudySession.query.filter_by(task_id=task.id).count() == 1

        assert set_task_status(user, task, "completed") is False
        assert set_task_status(user, task, "reopen") is True
        assert task.status == "pending"
        assert task.completed_at is None
        assert task.actual_minutes is None
        assert StudySession.query.filter_by(task_id=task.id).count() == 0
        assert TaskEvent.query.filter_by(task_id=task.id, event_type="reopened").count() == 1


def test_revision_completion_updates_revision_record(app, student_factory):
    user_id = student_factory(with_plan=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        task = StudyTask.query.filter_by(
            user_id=user.id, task_type="revision", status="pending"
        ).first()
        assert task is not None
        cycle = task.revision_cycle or 1
        chapter_id = task.syllabus_chapter_id

        set_task_status(user, task, "completed")

        record = RevisionRecord.query.filter_by(
            user_id=user.id,
            syllabus_chapter_id=chapter_id,
            revision_number=cycle,
        ).one()
        assert record.status in {"in_progress", "completed"}

        revision_tasks = StudyTask.query.filter_by(
            user_id=user.id,
            syllabus_chapter_id=chapter_id,
            task_type="revision",
            revision_cycle=cycle,
            status="pending",
        ).all()
        for item in revision_tasks:
            set_task_status(user, item, "completed")
        db.session.refresh(record)
        assert record.status == "completed"
        assert record.completed_date == date.today()

        set_task_status(user, task, "reopen")
        db.session.refresh(record)
        assert record.status == "pending"
        assert record.completed_date is None


def test_status_transition_rejects_invalid_and_cancelled(app, student_factory):
    user_id = student_factory()
    with app.app_context():
        user = db.session.get(User, user_id)
        task = _new_task(user)

        with pytest.raises(TaskTransitionError):
            set_task_status(user, task, "bogus")

        task.status = "cancelled"
        db.session.commit()
        with pytest.raises(TaskTransitionError):
            set_task_status(user, task, "completed")


def test_postpone_moves_to_conflict_free_slot(app, student_factory):
    user_id = student_factory(exam_in_days=90)
    with app.app_context():
        user = db.session.get(User, user_id)
        task = _new_task(user)
        old_date = task.scheduled_date

        moved, message = postpone_task(user, task, "next")

        assert moved is True
        assert task.scheduled_date > old_date
        assert task.status == "postponed"
        assert "moved" in message.lower()
        assert TaskEvent.query.filter_by(task_id=task.id, event_type="postponed").count() == 1


def test_postpone_rejects_past_requested_date(app, student_factory):
    user_id = student_factory()
    with app.app_context():
        user = db.session.get(User, user_id)
        task = _new_task(user)

        with pytest.raises(TaskTransitionError):
            postpone_task(user, task, "date", date.today())


def test_split_creates_non_overlapping_parts(app, student_factory):
    user_id = student_factory(exam_in_days=90)
    with app.app_context():
        user = db.session.get(User, user_id)
        task = _new_task(user, minutes=90)
        task_id = task.id

        ok, message = split_task(user, task, parts=2)

        assert ok is True
        assert "2" in message and "parts" in message
        parts = (
            StudyTask.query.filter_by(user_id=user.id, parent_task_id=task_id)
            .order_by(StudyTask.id)
            .all()
        )
        assert len(parts) == 1
        db.session.refresh(task)
        assert task.title.endswith("(part 1/2)")
        assert task.estimated_minutes + parts[0].estimated_minutes == 90
        assert task.status == "pending"
        assert parts[0].status == "pending"
        if task.scheduled_date == parts[0].scheduled_date:
            assert (
                time_to_minutes(task.end_time) <= time_to_minutes(parts[0].start_time)
                or time_to_minutes(parts[0].end_time) <= time_to_minutes(task.start_time)
            )
