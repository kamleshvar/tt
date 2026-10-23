from __future__ import annotations

from collections import defaultdict
from datetime import date, time, timedelta

from app.models import StudyPlan, StudyTask, SyllabusVersion, User
from app.planner.scheduler import time_to_minutes
from extensions import db

SCHOOL_START = time(8, 0)
SCHOOL_END = time(14, 0)
TRAVEL = 30


def register(client, email: str = "onboarding@example.com"):
    return client.post(
        "/register",
        data={
            "full_name": "Onboarding Student",
            "email": email,
            "password": "SecurePass123",
            "confirm_password": "SecurePass123",
            "student_class": "Class 12",
            "board": "CBSE",
            "academic_year": "2026-27",
        },
    )


def academic_data(exam_in_days: int = 150):
    return {
        "board": "CBSE",
        "academic_year": "2026-27",
        "exam_date": (date.today() + timedelta(days=exam_in_days)).isoformat(),
    }


def subject_data(subject_ids):
    data = {"subjects": [str(value) for value in subject_ids]}
    for index, subject_id in enumerate(subject_ids, start=1):
        data[f"confidence_{subject_id}"] = str((index % 5) + 1)
        data[f"performance_{subject_id}"] = "Consistent practice" if index == 1 else ""
    return data


def schedule_data(school=True):
    data = {
        "wake_time": "06:30",
        "sleep_time": "22:30",
        "school_travel_before": str(TRAVEL),
        "school_travel_after": str(TRAVEL),
        "tuition_travel_before": "0",
        "tuition_travel_after": "0",
    }
    if school:
        for day in range(5):
            data[f"school_start_{day}"] = "08:00"
            data[f"school_end_{day}"] = "14:00"
    return data


def preferences_data():
    return {
        "preferred_study_start": "17:00",
        "preferred_study_end": "22:00",
        "session_duration_minutes": "45",
        "break_duration_minutes": "10",
        "break_frequency_sessions": "2",
        "daily_cap_minutes": "240",
        "revision_frequency": "2",
        "lighter_days": ["6"],
        "struggle_notes": "Physics numericals need more practice.",
    }


def test_complete_onboarding_generates_a_valid_plan(client, app, version):
    register(client)
    with app.app_context():
        subject_ids = [
            item.id for item in db.session.get(SyllabusVersion, version).subjects[:3]
        ]

    assert client.post("/onboarding?step=1", data=academic_data()).status_code == 302
    assert client.post("/onboarding?step=2", data=subject_data(subject_ids)).status_code == 302
    assert client.post("/onboarding?step=3", data=schedule_data()).status_code == 302
    response = client.post("/onboarding?step=4", data=preferences_data())

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")

    with app.app_context():
        user = User.query.one()
        assert user.onboarding_complete
        assert user.profile.onboarding_step == 4
        assert user.profile.exam_date > date.today()
        assert len(user.subjects) == 3
        plan = StudyPlan.query.filter_by(user_id=user.id, status="active").one()
        tasks = StudyTask.query.filter(
            StudyTask.plan_id == plan.id, StudyTask.status != "cancelled"
        ).all()
        assert tasks
        assert {task.task_type for task in tasks} >= {"learning", "practice", "revision"}
        assert plan.generated_at is not None

        by_day = defaultdict(list)
        for task in tasks:
            assert task.scheduled_date >= date.today()
            assert task.start_time is not None
            assert task.end_time is not None
            assert time_to_minutes(task.end_time) > time_to_minutes(task.start_time)
            by_day[task.scheduled_date].append(task)

        for day, day_tasks in by_day.items():
            ordered = sorted(day_tasks, key=lambda item: time_to_minutes(item.start_time))
            for index in range(1, len(ordered)):
                assert time_to_minutes(ordered[index].start_time) >= time_to_minutes(
                    ordered[index - 1].end_time
                )
            if day.weekday() < 5:
                for task in day_tasks:
                    start = time_to_minutes(task.start_time)
                    end = time_to_minutes(task.end_time)
                    assert end <= time_to_minutes(SCHOOL_START) - TRAVEL or start >= time_to_minutes(
                        SCHOOL_END
                    ) + TRAVEL
            planned = sum(task.estimated_minutes for task in day_tasks)
            assert planned <= 240


def test_step_one_rejects_invalid_academic_year(client, app):
    register(client, "year@example.com")
    bad = academic_data()
    bad["academic_year"] = "2026/27"

    response = client.post("/onboarding?step=1", data=bad)

    assert response.status_code == 400
    with app.app_context():
        assert User.query.one().profile.onboarding_step == 1


def test_step_two_requires_at_least_one_subject(client, app):
    register(client, "nosubjects@example.com")
    client.post("/onboarding?step=1", data=academic_data())

    response = client.post("/onboarding?step=2", data={"subjects": []})

    assert response.status_code == 400
    assert b"Select at least one subject" in response.data


def test_step_two_reports_missing_syllabus(client, app):
    register(client, "nosyllabus@example.com")
    data = academic_data()
    data["board"] = "Other"
    data["academic_year"] = "2099-00"
    client.post("/onboarding?step=1", data=data)

    response = client.post("/onboarding?step=2", data={"subjects": ["1"]})

    assert response.status_code == 400
    assert b"No syllabus is configured" in response.data


def test_step_three_rejects_overlapping_commitments(client, app):
    register(client, "overlap@example.com")
    client.post("/onboarding?step=1", data=academic_data())
    with app.app_context():
        user = User.query.one()
        assert user.profile.onboarding_step == 2
    data = schedule_data()
    data["tuition_start_0"] = "13:30"
    data["tuition_end_0"] = "15:30"

    response = client.post("/onboarding?step=3", data=data)

    assert response.status_code == 400
    assert b"overlapping commitments" in response.data


def test_completed_onboarding_redirects_away(client, onboarded_student, login_student):
    login_student()

    response = client.get("/onboarding")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
