from __future__ import annotations

from datetime import date, time, timedelta

import pytest

from app import create_app
from app.models import (
    ExamDate,
    StudentPreferences,
    StudentSubject,
    SyllabusVersion,
    User,
    WeeklyCommitment,
)
from app.planner.engine import generate_study_plan
from app.services.accounts import create_student
from app.syllabus.loader import seed_default_syllabus
from extensions import db

TEST_PASSWORD = "StudentPass123"


@pytest.fixture()
def app(tmp_path):
    database = tmp_path / "test.db"
    application = create_app(
        "testing",
        {
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database.as_posix()}",
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
            "SERVER_NAME": None,
        },
    )
    with application.app_context():
        db.create_all()
        seed_default_syllabus()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def version(app):
    with app.app_context():
        item = SyllabusVersion.query.filter_by(is_current=True).first()
        return item.id


@pytest.fixture()
def student_factory(app):
    def make(
        *,
        email: str = "student@example.com",
        full_name: str = "Asha Rao",
        password: str = TEST_PASSWORD,
        board: str = "CBSE",
        academic_year: str = "2026-27",
        onboarded: bool = True,
        subject_count: int = 3,
        with_plan: bool = False,
        exam_in_days: int | None = 140,
        daily_cap: int = 240,
    ):
        with app.app_context():
            existing = User.query.filter_by(email=email).first()
            if existing is not None:
                return existing.id
            user = create_student(
                full_name=full_name,
                email=email,
                password=password,
                board=board,
                academic_year=academic_year,
            )
            profile = user.profile
            profile.onboarding_completed = onboarded
            profile.onboarding_step = 4 if onboarded else 1
            if exam_in_days is not None:
                exam_day = date.today() + timedelta(days=exam_in_days)
                profile.exam_date = exam_day
                db.session.add(
                    ExamDate(
                        user_id=user.id,
                        title="Board Examination",
                        exam_type="board",
                        exam_date=exam_day,
                        is_primary=True,
                    )
                )
            preferences = user.preferences or StudentPreferences(user_id=user.id)
            user.preferences = preferences
            preferences.wake_time = time(6, 30)
            preferences.sleep_time = time(22, 30)
            preferences.preferred_study_start = time(17, 0)
            preferences.preferred_study_end = time(22, 0)
            preferences.session_duration_minutes = 45
            preferences.break_duration_minutes = 10
            preferences.break_frequency_sessions = 2
            preferences.daily_cap_minutes = daily_cap
            preferences.revision_frequency = 2
            preferences.lighter_days = [6]
            for weekday in range(5):
                db.session.add(
                    WeeklyCommitment(
                        user_id=user.id,
                        day_of_week=weekday,
                        commitment_type="school",
                        title="School",
                        start_time=time(8, 0),
                        end_time=time(14, 0),
                        travel_before_minutes=30,
                        travel_after_minutes=30,
                    )
                )
            syllabus = SyllabusVersion.query.filter_by(
                board=board, academic_year=academic_year, is_current=True
            ).first()
            if syllabus is not None and subject_count:
                for subject in syllabus.subjects[:subject_count]:
                    db.session.add(
                        StudentSubject(
                            user_id=user.id,
                            syllabus_subject_id=subject.id,
                            confidence=3,
                            is_active=True,
                        )
                    )
            db.session.commit()
            if with_plan and onboarded:
                generate_study_plan(user)
            return user.id

    return make


@pytest.fixture()
def onboarded_student(student_factory):
    return student_factory(with_plan=True)


@pytest.fixture()
def login_student(client):
    def login(email: str = "student@example.com", password: str = TEST_PASSWORD):
        return client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )

    return login


@pytest.fixture()
def login_admin(client):
    def login(password: str = "test-admin-password"):
        return client.post(
            "/admin/",
            data={"password": password},
            follow_redirects=False,
        )

    return login
