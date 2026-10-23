from __future__ import annotations

from app.models import SyllabusChapter, SyllabusSubject, SyllabusVersion
from app.syllabus.loader import read_syllabus_payload
from app.syllabus.validator import validate_syllabus_payload


def test_bundled_dataset_is_structurally_valid():
    payload = read_syllabus_payload(
        "data/syllabus/cbse_class12_2026_27.json"
    )
    report = validate_syllabus_payload(payload)

    assert report["valid"] is True
    assert report["subject_count"] == 11
    assert report["chapter_count"] == 130


def test_seeded_version_has_workload_stages_for_every_chapter(app):
    with app.app_context():
        version = SyllabusVersion.query.filter_by(is_current=True).one()
        subjects = SyllabusSubject.query.filter_by(
            version_id=version.id, is_removed=False
        ).all()
        chapters = (
            SyllabusChapter.query.join(SyllabusChapter.unit)
            .filter(SyllabusChapter.is_removed.is_(False))
            .all()
        )

        assert len(subjects) == 11
        assert len(chapters) == 130
        assert version.chapter_count == 130
        assert version.research_status == "inferred_workload_hours"
        assert version.research_sources
        for chapter in chapters:
            assert chapter.learning_minutes > 0
            assert chapter.practice_minutes > 0
            assert chapter.revision_minutes >= 0
            assert chapter.total_minutes == chapter.estimated_minutes
