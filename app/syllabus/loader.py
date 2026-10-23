import hashlib
import json
from datetime import date
from pathlib import Path

from app.models import (
    SyllabusChapter,
    SyllabusSubject,
    SyllabusTopic,
    SyllabusUnit,
    SyllabusVersion,
    utcnow,
)
from app.syllabus.validator import canonical_key, validate_syllabus_payload
from extensions import db


class SyllabusImportError(Exception):
    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def read_syllabus_payload(path) -> dict:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise SyllabusImportError(f"Syllabus file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SyllabusImportError(f"Invalid JSON in syllabus file: {exc}") from exc


def dataset_checksum(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sync_topics(chapter: SyllabusChapter, topics: list) -> None:
    existing = {topic.canonical_key: topic for topic in chapter.topics}
    seen = set()
    for index, item in enumerate(topics or []):
        key = canonical_key(item.get("name", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        topic = existing.get(key)
        if topic is None:
            topic = SyllabusTopic(chapter=chapter, canonical_key=key)
            db.session.add(topic)
        topic.name = item["name"].strip()
        topic.order_index = index
        topic.estimated_minutes = item.get("estimated_minutes")
        topic.is_optional = bool(item.get("is_optional", False))
        topic.is_removed = False
    for key, topic in existing.items():
        if key not in seen:
            topic.is_removed = True


def _hours_to_minutes(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return None
    if hours < 0:
        return None
    return int(round(hours * 60))


def _parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _chapter_stage_minutes(item: dict) -> tuple[int, int, int, int]:
    learning = _hours_to_minutes(item.get("learning_hours"))
    practice = _hours_to_minutes(item.get("practice_hours"))
    revision = _hours_to_minutes(item.get("revision_hours"))
    explicit_total = _hours_to_minutes(item.get("total_hours"))
    stage_total = (learning or 0) + (practice or 0) + (revision or 0)
    fallback = int(item.get("estimated_minutes") or 180)
    if stage_total <= 0:
        estimated = max(1, fallback)
        learning = int(round(estimated * 0.55))
        practice = int(round(estimated * 0.30))
        revision = estimated - learning - practice
        return learning, practice, revision, estimated
    total = explicit_total or stage_total or fallback
    return learning or 0, practice or 0, revision or 0, max(total, stage_total)


def _sync_chapters(unit: SyllabusUnit, chapters: list) -> None:
    existing = {chapter.canonical_key: chapter for chapter in unit.chapters}
    seen = set()
    for index, item in enumerate(chapters or []):
        key = canonical_key(item.get("name", ""))
        if not key or key in seen:
            continue
        seen.add(key)
        chapter = existing.get(key)
        if chapter is None:
            chapter = SyllabusChapter(unit=unit, canonical_key=key)
            db.session.add(chapter)
        learning, practice, revision, total = _chapter_stage_minutes(item)
        chapter.name = item["name"].strip()
        chapter.order_index = index
        chapter.estimated_minutes = total
        chapter.learning_minutes = learning
        chapter.practice_minutes = practice
        chapter.revision_minutes = revision
        chapter.weightage_marks = item.get("weightage_marks")
        chapter.is_optional = bool(item.get("is_optional", False))
        chapter.is_removed = False
        chapter.difficulty = str(item.get("difficulty") or "medium").lower()
        chapter.estimate_confidence = str(
            item.get("estimate_confidence") or "medium"
        ).lower()
        chapter.estimate_method = item.get("estimate_method")
        chapter.research_status = str(
            item.get("research_status") or "unverified"
        ).lower()
        chapter.research_sources = item.get("research_sources") or []
        chapter.research_notes = item.get("research_notes")
        chapter.last_researched_at = _parse_date(item.get("last_researched_at"))
        _sync_topics(chapter, item.get("topics", []))
    for key, chapter in existing.items():
        if key not in seen:
            chapter.is_removed = True


def _sync_units(subject: SyllabusSubject, units: list) -> None:
    existing = {unit.name.strip().lower(): unit for unit in subject.units}
    seen = set()
    for index, item in enumerate(units or []):
        name = item.get("name", "").strip()
        key = name.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        unit = existing.get(key)
        if unit is None:
            unit = SyllabusUnit(subject=subject)
            db.session.add(unit)
        unit.name = name
        unit.order_index = index
        _sync_chapters(unit, item.get("chapters", []))
    for key, unit in existing.items():
        if key not in seen:
            for chapter in unit.chapters:
                chapter.is_removed = True


def import_syllabus(
    payload: dict,
    *,
    source_file: str | None = None,
    make_current: bool = True,
) -> SyllabusVersion:
    report = validate_syllabus_payload(payload)
    if not report["valid"]:
        raise SyllabusImportError(
            "Syllabus validation failed. No data was imported.", report["errors"]
        )
    metadata = payload["metadata"]
    board = metadata["board"].strip()
    academic_year = metadata["academic_year"].strip()
    version_label = metadata["version_label"].strip()
    checksum = dataset_checksum(payload)

    try:
        version = SyllabusVersion.query.filter_by(
            board=board, academic_year=academic_year, version_label=version_label
        ).first()
        if version is None:
            version = SyllabusVersion(
                board=board,
                academic_year=academic_year,
                version_label=version_label,
            )
            db.session.add(version)
            db.session.flush()
        version.status = str(metadata.get("status") or "draft").lower()
        version.source_file = source_file
        version.checksum = checksum
        version.notes = metadata.get("notes")
        version.research_status = str(
            metadata.get("research_status") or "unverified"
        ).lower()
        version.research_sources = metadata.get("research_sources") or metadata.get(
            "sources"
        ) or []
        version.last_researched_at = _parse_date(metadata.get("last_researched_at"))
        if version.status == "validated":
            version.validated_at = version.validated_at or utcnow()

        existing_code = {subject.code.lower(): subject for subject in version.subjects}
        existing_name = {
            subject.name.strip().lower(): subject for subject in version.subjects
        }
        seen_ids = set()
        for index, item in enumerate(payload["subjects"]):
            code = item["code"].strip()
            name = item["name"].strip()
            subject = existing_code.get(code.lower()) or existing_name.get(
                name.lower()
            )
            if subject is None:
                subject = SyllabusSubject(version=version, code=code, name=name)
                db.session.add(subject)
            subject.code = code
            subject.name = name
            if not subject.id:
                db.session.flush()
            seen_ids.add(subject.id)
            subject.order_index = index
            subject.is_removed = False
            _sync_units(subject, item.get("units", []))
        for subject in version.subjects:
            if subject.id not in seen_ids:
                subject.is_removed = True
                for unit in subject.units:
                    for chapter in unit.chapters:
                        chapter.is_removed = True

        if make_current:
            SyllabusVersion.query.filter(
                SyllabusVersion.board == board,
                SyllabusVersion.academic_year == academic_year,
                SyllabusVersion.id != version.id,
            ).update({"is_current": False}, synchronize_session=False)
            version.is_current = True
        db.session.commit()
        return version
    except Exception:
        db.session.rollback()
        raise


def seed_default_syllabus(path=None) -> SyllabusVersion:
    from flask import current_app

    source = Path(path or current_app.config["DEFAULT_SYLLABUS_FILE"])
    payload = read_syllabus_payload(source)
    return import_syllabus(
        payload,
        source_file=str(source.relative_to(source.parent.parent.parent))
        if source.is_relative_to(source.parent.parent.parent)
        else str(source),
        make_current=True,
    )


def current_syllabus_version(board: str, academic_year: str) -> SyllabusVersion | None:
    return (
        SyllabusVersion.query.filter_by(
            board=board, academic_year=academic_year, is_current=True
        )
        .order_by(SyllabusVersion.imported_at.desc())
        .first()
    )
