from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.models import StudyTask, StudyPlan, SyllabusVersion, User, WeeklyCommitment
from app.planner.scheduler import (
    find_slot,
    get_study_slots,
    sleep_minutes,
    time_to_minutes,
    validate_time_settings,
)
from extensions import db


def future_day(weekday: int) -> date:
    today = date.today()
    delta = (weekday - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


def test_slots_respect_school_travel_preferences_and_daily_cap(app, student_factory):
    user_id = student_factory()
    day = future_day(0)
    with app.app_context():
        user = db.session.get(User, user_id)
        user.preferences.daily_cap_minutes = 90
        db.session.commit()
        midnight = datetime.combine(day, time(0, 0))
        slots = get_study_slots(user, day, now=midnight)

        assert slots
        assert all(slot.start >= time_to_minutes(time(17, 0)) for slot in slots)
        assert all(slot.end <= time_to_minutes(time(22, 0)) for slot in slots)
        assert sum(slot.minutes for slot in slots) <= 90
        for slot in slots:
            assert slot.end <= time_to_minutes(time(8, 0)) - 30 or slot.start >= time_to_minutes(
                time(14, 0)
            ) + 30


def test_break_frequency_controls_gaps(app, student_factory):
    user_id = student_factory()
    day = future_day(5)
    with app.app_context():
        user = db.session.get(User, user_id)
        WeeklyCommitment.query.filter_by(user_id=user.id).delete()
        preferences = user.preferences
        preferences.preferred_study_start = time(17, 0)
        preferences.preferred_study_end = time(19, 0)
        preferences.session_duration_minutes = 30
        preferences.break_duration_minutes = 10
        preferences.break_frequency_sessions = 2
        preferences.daily_cap_minutes = 240
        preferences.lighter_days = []
        db.session.commit()

        slots = get_study_slots(user, day, now=datetime.combine(day, time(0, 0)))

        assert [(slot.start, slot.end) for slot in slots] == [
            (17 * 60, 17 * 60 + 30),
            (17 * 60 + 30, 18 * 60),
            (18 * 60 + 10, 18 * 60 + 40),
            (18 * 60 + 40, 19 * 60),
        ]


def test_existing_tasks_reduce_capacity_and_are_not_overlapped(app, student_factory):
    user_id = student_factory()
    day = future_day(0)
    with app.app_context():
        user = db.session.get(User, user_id)
        version = SyllabusVersion.query.filter_by(is_current=True).first()
        plan = StudyPlan(
            user_id=user.id,
            syllabus_version_id=version.id,
            version_number=1,
            status="active",
            horizon_start=date.today(),
            horizon_end=date.today() + timedelta(days=60),
        )
        db.session.add(plan)
        db.session.flush()
        task = StudyTask(
            user_id=user.id,
            plan_id=plan.id,
            syllabus_chapter_id=None,
            task_type="practice",
            title="Planned task",
            original_scheduled_date=day,
            scheduled_date=day,
            start_time=time(17, 0),
            end_time=time(18, 0),
            estimated_minutes=60,
            priority=2,
            status="pending",
        )
        db.session.add(task)
        user.preferences.daily_cap_minutes = 180
        db.session.commit()

        slots = get_study_slots(user, day, now=datetime.combine(day, time(0, 0)))

        assert sum(slot.minutes for slot in slots) <= 120
        for slot in slots:
            assert slot.end <= time_to_minutes(time(17, 0)) or slot.start >= time_to_minutes(
                time(18, 0)
            )


def test_find_slot_returns_a_fitting_window(app, student_factory):
    user_id = student_factory()
    day = future_day(0)
    with app.app_context():
        user = db.session.get(User, user_id)
        slot = find_slot(
            user,
            45,
            day,
            day,
            now=datetime.combine(day, time(0, 0)),
        )
        assert slot is not None
        assert slot.minutes == 45
        assert slot.day == day
        assert slot.start >= time_to_minutes(time(17, 0))

        too_large = find_slot(
            user,
            600,
            day,
            day,
            now=datetime.combine(day, time(0, 0)),
        )
        assert too_large is None


def test_time_validation_helpers():
    assert sleep_minutes(time(6, 30), time(22, 30)) == 480
    errors = validate_time_settings(
        wake=time(6, 30),
        sleep=time(22, 30),
        preferred_start=time(17, 0),
        preferred_end=time(22, 0),
        session_minutes=45,
        break_minutes=10,
        daily_cap=240,
        revision_frequency=2,
        lighter_days=[6],
    )
    assert errors == []

    errors = validate_time_settings(
        wake=time(22, 0),
        sleep=time(1, 0),
        preferred_start=time(23, 0),
        preferred_end=time(22, 0),
        session_minutes=10,
        break_minutes=0,
        daily_cap=10,
        revision_frequency=5,
        lighter_days=[9],
    )
    assert len(errors) >= 5
