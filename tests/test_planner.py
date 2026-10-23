from __future__ import annotations

from collections import defaultdict
from datetime import date, time

from app.models import MockTest, StudyPlan, StudyTask, User
from app.planner.engine import generate_study_plan
from app.planner.progress import progress_snapshot
from app.planner.scheduler import time_to_minutes
from app.services.task_management import set_task_status
from extensions import db


def test_generated_plan_has_no_task_overlaps(app, student_factory):
    user_id = student_factory(with_plan=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        plan = StudyPlan.query.filter_by(user_id=user.id, status="active").one()
        tasks = StudyTask.query.filter_by(plan_id=plan.id).all()
        assert tasks
        assert all(task.plan_id == plan.id for task in tasks)
        assert plan.horizon_start == date.today()
        assert plan.horizon_end == user.profile.exam_date
        assert plan.parameters["chapters_selected"] > 0

        by_day = defaultdict(list)
        for task in tasks:
            by_day[task.scheduled_date].append(task)
        for day_tasks in by_day.values():
            ordered = sorted(day_tasks, key=lambda item: time_to_minutes(item.start_time))
            for previous, current in zip(ordered, ordered[1:]):
                assert time_to_minutes(current.start_time) >= time_to_minutes(previous.end_time)
            assert sum(task.estimated_minutes for task in day_tasks) <= 240


def test_regeneration_preserves_completed_work_and_stale_mocks(app, student_factory):
    user_id = student_factory(with_plan=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        old_plan = StudyPlan.query.filter_by(user_id=user.id, status="active").one()
        completed = (
            StudyTask.query.filter_by(plan_id=old_plan.id, status="pending")
            .filter(StudyTask.task_type.in_(["learning", "practice"]))
            .first()
        )
        assert completed is not None
        set_task_status(user, completed, "completed")
        completed_id = completed.id
        pending_old_mocks = MockTest.query.filter_by(
            user_id=user.id, status="pending"
        ).all()
        assert pending_old_mocks

        new_plan = generate_study_plan(user, regenerate=True)
        db.session.refresh(old_plan)

        assert old_plan.status == "superseded"
        assert new_plan.status == "active"
        assert new_plan.version_number == old_plan.version_number + 1
        preserved = db.session.get(StudyTask, completed_id)
        assert preserved.status == "completed"
        assert preserved.plan_id == old_plan.id
        old_open = StudyTask.query.filter(
            StudyTask.plan_id == old_plan.id,
            StudyTask.status.in_(["pending", "in_progress", "postponed", "overdue"]),
        ).count()
        assert old_open == 0
        for mock in pending_old_mocks:
            db.session.refresh(mock)
            assert mock.status == "cancelled"
        new_mocks = MockTest.query.filter_by(user_id=user.id, status="pending").all()
        assert new_mocks
        assert all(mock.task and mock.task.plan_id == new_plan.id for mock in new_mocks)
        assert any("preserved" in warning for warning in new_plan.warnings)


def test_capacity_shortage_is_reported_not_silently_dropped(app, student_factory):
    user_id = student_factory(
        onboarded=True,
        subject_count=6,
        exam_in_days=30,
        daily_cap=45,
    )
    with app.app_context():
        user = db.session.get(User, user_id)
        plan = generate_study_plan(user)

        assert plan.feasible is False
        assert plan.parameters["unplaced_count"] > 0
        assert any("could not be placed" in warning for warning in plan.warnings)
        assert any("capacity" in warning.lower() for warning in plan.warnings)


def test_mock_warning_when_exam_is_too_close(app, student_factory):
    user_id = student_factory(exam_in_days=2)
    with app.app_context():
        user = db.session.get(User, user_id)
        plan = generate_study_plan(user)

        assert any("No mock examination" in warning for warning in plan.warnings)
        assert MockTest.query.filter_by(user_id=user.id).count() == 0


def test_progress_metrics_stay_independent(app, student_factory):
    user_id = student_factory(with_plan=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        learning = StudyTask.query.filter_by(
            user_id=user.id, task_type="learning", status="pending"
        ).first()
        set_task_status(user, learning, "completed")
        db.session.expire_all()
        snapshot = progress_snapshot(user)

        assert snapshot["tasks"]["completed"] >= 1
        assert snapshot["syllabus_coverage"] > 0
        assert snapshot["learning_completion"] > 0
        assert snapshot["practice_completion"] < snapshot["learning_completion"]
        assert snapshot["mock_total"] >= 1
        assert snapshot["free_minutes_next_7_days"] >= 0
