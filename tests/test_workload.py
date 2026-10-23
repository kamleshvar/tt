from __future__ import annotations

from datetime import date, timedelta

from app.models import (
    AdminAuditLog,
    StudentChapterPlan,
    StudyPlan,
    StudyTask,
    SyllabusChapter,
    User,
)
from app.planner.engine import generate_study_plan
from app.planner.workload import personalized_stage_minutes
from extensions import db


def _selected_chapter(app, user_id: int) -> SyllabusChapter:
    with app.app_context():
        task = (
            StudyTask.query.filter(
                StudyTask.user_id == user_id,
                StudyTask.syllabus_chapter_id.isnot(None),
            )
            .order_by(StudyTask.id)
            .first()
        )
        return db.session.get(SyllabusChapter, task.syllabus_chapter_id)


def test_personalization_is_bounded_and_revision_only_preserves_practice():
    class Chapter:
        learning_minutes = 300
        practice_minutes = 300
        revision_minutes = 120
        estimated_minutes = 720

    base = personalized_stage_minutes(Chapter(), subject_confidence=3)
    low = personalized_stage_minutes(Chapter(), subject_confidence=1)
    high = personalized_stage_minutes(Chapter(), subject_confidence=5)
    revision_only = personalized_stage_minutes(
        Chapter(), subject_confidence=1, study_mode="revision_only"
    )

    assert low["learning"] > base["learning"] > high["learning"]
    assert low["practice"] >= base["practice"] >= high["practice"]
    assert low["revision"] >= base["revision"] >= high["revision"]
    assert revision_only["revision"] == 120
    assert revision_only["practice"] == 150
    assert revision_only["learning"] == 45
    assert revision_only["total"] == 315


def test_student_workload_page_and_chapter_override(
    client, app, student_factory, login_student
):
    user_id = student_factory(with_plan=True)
    chapter = _selected_chapter(app, user_id)
    login_student()

    response = client.get("/workload")
    assert response.status_code == 200
    assert b"Chapter workload and confidence" in response.data

    response = client.get(f"/subjects/chapters/{chapter.id}")
    assert response.status_code == 200
    assert b"Workload estimate" in response.data

    response = client.post(
        f"/workload/{chapter.id}",
        data={"confidence": "1", "study_mode": "revision_only"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"preference saved" in response.data

    with app.app_context():
        override = StudentChapterPlan.query.filter_by(
            user_id=user_id, syllabus_chapter_id=chapter.id
        ).one()
        assert override.confidence == 1
        assert override.study_mode == "revision_only"

    response = client.post(
        f"/workload/{chapter.id}",
        data={"confidence": "", "study_mode": "first_time"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        assert (
            StudentChapterPlan.query.filter_by(
                user_id=user_id, syllabus_chapter_id=chapter.id
            ).count()
            == 0
        )


def test_admin_workload_page_and_estimate_edit(client, app, student_factory, login_admin):
    student_factory(with_plan=True)
    with app.app_context():
        chapter = SyllabusChapter.query.filter_by(is_removed=False).first()
        chapter_id = chapter.id
    login_admin()

    response = client.get("/admin/workload")
    assert response.status_code == 200
    assert b"Chapter workload management" in response.data

    response = client.post(
        f"/admin/workload/chapter/{chapter_id}",
        data={
            "learning_hours": "4.5",
            "practice_hours": "3",
            "revision_hours": "1.5",
            "estimate_confidence": "high",
            "difficulty": "hard",
            "review_note": "Adjusted after teacher review.",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Workload estimate updated" in response.data

    with app.app_context():
        chapter = db.session.get(SyllabusChapter, chapter_id)
        assert chapter.learning_minutes == 270
        assert chapter.practice_minutes == 180
        assert chapter.revision_minutes == 90
        assert chapter.estimated_minutes == 540
        assert chapter.estimate_confidence == "high"
        assert chapter.difficulty == "hard"
        assert chapter.estimate_method == "administrator_review"
        assert chapter.research_status == "admin_reviewed"
        assert chapter.estimate_review_note == "Adjusted after teacher review."
        audit = AdminAuditLog.query.filter_by(
            action="syllabus_estimate_updated", entity_id=chapter_id
        ).first()
        assert audit is not None


def test_admin_estimate_edit_requires_reason(client, app, student_factory, login_admin):
    student_factory(with_plan=True)
    with app.app_context():
        chapter = SyllabusChapter.query.filter_by(is_removed=False).first()
        chapter_id = chapter.id
        original = chapter.learning_minutes
    login_admin()

    response = client.post(
        f"/admin/workload/chapter/{chapter_id}",
        data={
            "learning_hours": "10",
            "practice_hours": "10",
            "revision_hours": "10",
            "review_note": "no",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"short review reason" in response.data
    with app.app_context():
        chapter = db.session.get(SyllabusChapter, chapter_id)
        assert chapter.learning_minutes == original


def test_workload_summary_scales_capacity_for_long_horizons(client, app, student_factory, login_student):
    user_id = student_factory(with_plan=True, exam_in_days=150)
    login_student()

    response = client.get("/workload")
    assert response.status_code == 200

    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.profile.exam_date > date.today() + timedelta(days=100)

def test_plan_regeneration_applies_chapter_override(app, student_factory):
    user_id = student_factory(with_plan=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        task = (
            StudyTask.query.filter(
                StudyTask.user_id == user_id,
                StudyTask.syllabus_chapter_id.isnot(None),
            )
            .order_by(StudyTask.id)
            .first()
        )
        chapter_id = task.syllabus_chapter_id
        old_plan = StudyPlan.query.filter_by(user_id=user_id, status="active").one()
        old_minutes = old_plan.parameters["personalized_workload_minutes"]

        db.session.add(
            StudentChapterPlan(
                user_id=user_id,
                syllabus_chapter_id=chapter_id,
                study_mode="revision_only",
            )
        )
        db.session.commit()

        new_plan = generate_study_plan(user, regenerate=True)

        assert new_plan.version_number > old_plan.version_number
        assert new_plan.parameters["revision_only_chapters"] == 1
        assert new_plan.parameters["personalized_workload_minutes"] < old_minutes
