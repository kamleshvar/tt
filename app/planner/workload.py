from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func

from app.models import (
    OPEN_TASK_STATUSES,
    StudentChapterPlan,
    StudyTask,
)
from app.planner.progress import selected_chapters
from app.planner.scheduler import available_minutes
from extensions import db

STAGE_LABELS = {
    "learning": "Learning",
    "practice": "Practice",
    "revision": "Revision",
}

CONFIDENCE_LABELS = {
    1: "Very low",
    2: "Low",
    3: "Moderate",
    4: "High",
    5: "Very high",
}

LEARNING_FACTORS = {1: 1.25, 2: 1.15, 3: 1.0, 4: 0.90, 5: 0.80}
PRACTICE_FACTORS = {1: 1.10, 2: 1.05, 3: 1.0, 4: 1.0, 5: 1.0}
REVISION_FACTORS = {1: 1.05, 2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0}


def _effective_confidence(subject_confidence, override) -> int:
    value = override.confidence if override and override.confidence else subject_confidence
    try:
        return max(1, min(5, int(value or 3)))
    except (TypeError, ValueError):
        return 3


def base_stage_minutes(chapter) -> dict:
    learning = int(chapter.learning_minutes or 0)
    practice = int(chapter.practice_minutes or 0)
    revision = int(chapter.revision_minutes or 0)
    if learning + practice + revision <= 0:
        total = max(1, int(chapter.estimated_minutes or 180))
        learning = int(round(total * 0.55))
        practice = int(round(total * 0.30))
        revision = max(0, total - learning - practice)
    return {"learning": learning, "practice": practice, "revision": revision}


def personalized_stage_minutes(
    chapter,
    *,
    subject_confidence=3,
    override=None,
    study_mode: str | None = None,
) -> dict:
    base = base_stage_minutes(chapter)
    confidence = _effective_confidence(subject_confidence, override)
    mode = (study_mode or (override.study_mode if override else None) or "first_time").lower()
    if mode not in {"first_time", "revision_only"}:
        mode = "first_time"
    if mode == "revision_only":
        learning = int(round(base["learning"] * 0.15)) if base["learning"] else 0
        practice = int(round(base["practice"] * 0.50))
        revision = base["revision"]
    else:
        learning = int(round(base["learning"] * LEARNING_FACTORS[confidence]))
        practice = int(round(base["practice"] * PRACTICE_FACTORS[confidence]))
        revision = int(round(base["revision"] * REVISION_FACTORS[confidence]))
    result = {
        "learning": max(0, learning),
        "practice": max(0, practice),
        "revision": max(0, revision),
        "confidence": confidence,
        "study_mode": mode,
    }
    result["total"] = result["learning"] + result["practice"] + result["revision"]
    return result


def _completed_stage_minutes(user_id: int) -> dict[int, dict[str, int]]:
    rows = (
        db.session.query(
            StudyTask.syllabus_chapter_id,
            StudyTask.task_type,
            func.coalesce(func.sum(func.coalesce(StudyTask.actual_minutes, StudyTask.estimated_minutes)), 0),
        )
        .filter(
            StudyTask.user_id == user_id,
            StudyTask.status == "completed",
            StudyTask.syllabus_chapter_id.isnot(None),
        )
        .group_by(StudyTask.syllabus_chapter_id, StudyTask.task_type)
        .all()
    )
    completed: dict[int, dict[str, int]] = {}
    for chapter_id, task_type, minutes in rows:
        if task_type not in {"learning", "practice", "revision"}:
            continue
        completed.setdefault(chapter_id, {"learning": 0, "practice": 0, "revision": 0})
        completed[chapter_id][task_type] += int(minutes or 0)
    return completed


def _task_planned_minutes(user) -> dict[int, dict[str, int]]:
    rows = (
        db.session.query(
            StudyTask.syllabus_chapter_id,
            StudyTask.task_type,
            func.coalesce(func.sum(StudyTask.estimated_minutes), 0),
        )
        .filter(
            StudyTask.user_id == user.id,
            StudyTask.status != "cancelled",
            StudyTask.syllabus_chapter_id.isnot(None),
        )
        .group_by(StudyTask.syllabus_chapter_id, StudyTask.task_type)
        .all()
    )
    planned: dict[int, dict[str, int]] = {}
    for chapter_id, task_type, minutes in rows:
        planned.setdefault(chapter_id, {"learning": 0, "practice": 0, "revision": 0})
        if task_type in planned[chapter_id]:
            planned[chapter_id][task_type] += int(minutes or 0)
    return planned


def _capacity_minutes(user) -> int:
    preferences = user.preferences
    profile = user.profile
    if preferences is None:
        return 0
    today = date.today()
    horizon = profile.exam_date if profile and profile.exam_date else today
    if horizon < today:
        horizon = today
    total_days = (horizon - today).days + 1
    sample_days = min(total_days, 28)
    sample_end = today + timedelta(days=sample_days - 1)
    free = available_minutes(user, today, sample_end)
    if total_days <= sample_days:
        open_planned = int(
            StudyTask.query.filter(
                StudyTask.user_id == user.id,
                StudyTask.status.in_(OPEN_TASK_STATUSES),
                StudyTask.scheduled_date >= today,
                StudyTask.scheduled_date <= horizon,
            )
            .with_entities(func.coalesce(func.sum(StudyTask.estimated_minutes), 0))
            .scalar()
            or 0
        )
        return free + open_planned
    scaled_free = int(round(free / sample_days * total_days))
    open_after_sample = int(
        StudyTask.query.filter(
            StudyTask.user_id == user.id,
            StudyTask.status.in_(OPEN_TASK_STATUSES),
            StudyTask.scheduled_date > sample_end,
            StudyTask.scheduled_date <= horizon,
        )
        .with_entities(func.coalesce(func.sum(StudyTask.estimated_minutes), 0))
        .scalar()
        or 0
    )
    return scaled_free + open_after_sample


def workload_rows(user, *, include_at_risk: bool = True) -> dict:
    chapters = selected_chapters(user)
    if not chapters:
        return {
            "rows": [],
            "summary": {
                "chapters_total": 0,
                "base_minutes": 0,
                "personalized_minutes": 0,
                "completed_minutes": 0,
                "remaining_minutes": 0,
                "learning_remaining": 0,
                "practice_remaining": 0,
                "revision_remaining": 0,
                "capacity_minutes": 0,
                "feasible": True,
                "at_risk_count": 0,
                "low_confidence_count": 0,
                "revision_only_count": 0,
            },
        }
    links = {link.syllabus_subject_id: link for link in user.subjects}
    overrides = {
        item.syllabus_chapter_id: item
        for item in StudentChapterPlan.query.filter_by(user_id=user.id).all()
    }
    completed = _completed_stage_minutes(user.id)
    planned = _task_planned_minutes(user)
    rows = []
    for chapter in chapters:
        subject = chapter.subject
        link = links.get(subject.id) if subject else None
        override = overrides.get(chapter.id)
        subject_confidence = link.confidence if link else 3
        base = base_stage_minutes(chapter)
        personalized = personalized_stage_minutes(
            chapter, subject_confidence=subject_confidence, override=override
        )
        done = completed.get(
            chapter.id, {"learning": 0, "practice": 0, "revision": 0}
        )
        remaining = {
            stage: max(0, personalized[stage] - done.get(stage, 0))
            for stage in ("learning", "practice", "revision")
        }
        total_remaining = sum(remaining.values())
        if total_remaining == 0:
            status = "Complete"
        elif done["learning"] < personalized["learning"]:
            status = "Learning in progress" if done["learning"] else "Not started"
        elif done["practice"] < personalized["practice"]:
            status = "Practice in progress" if done["practice"] else "Learning complete"
        elif done["revision"] < personalized["revision"]:
            status = "Revision in progress" if done["revision"] else "Revision due"
        else:
            status = "In progress"
        rows.append(
            {
                "chapter": chapter,
                "subject": subject,
                "base": base,
                "personalized": personalized,
                "done": done,
                "remaining": remaining,
                "total_remaining": total_remaining,
                "base_total": sum(base.values()),
                "personalized_total": personalized["total"],
                "confidence": personalized["confidence"],
                "confidence_label": CONFIDENCE_LABELS[personalized["confidence"]],
                "study_mode": personalized["study_mode"],
                "override": override,
                "status": status,
                "planned": planned.get(
                    chapter.id, {"learning": 0, "practice": 0, "revision": 0}
                ),
            }
        )

    capacity = _capacity_minutes(user)
    summary = {
        "chapters_total": len(rows),
        "base_minutes": sum(row["base_total"] for row in rows),
        "personalized_minutes": sum(row["personalized_total"] for row in rows),
        "completed_minutes": sum(sum(row["done"].values()) for row in rows),
        "remaining_minutes": sum(row["total_remaining"] for row in rows),
        "learning_remaining": sum(row["remaining"]["learning"] for row in rows),
        "practice_remaining": sum(row["remaining"]["practice"] for row in rows),
        "revision_remaining": sum(row["remaining"]["revision"] for row in rows),
        "capacity_minutes": capacity,
        "feasible": sum(row["total_remaining"] for row in rows) <= capacity,
        "at_risk_count": 0,
        "low_confidence_count": sum(
            1
            for row in rows
            if row["chapter"].estimate_confidence == "low"
            or row["confidence"] <= 2
        ),
        "revision_only_count": sum(
            1 for row in rows if row["study_mode"] == "revision_only"
        ),
    }

    if include_at_risk:
        budget = capacity
        order = sorted(
            rows,
            key=lambda row: (
                -float(row["chapter"].weightage_marks or 0),
                row["confidence"],
                row["chapter"].order_index,
            ),
        )
        for row in order:
            if row["total_remaining"] <= 0:
                row["at_risk"] = False
                continue
            row["at_risk"] = row["total_remaining"] > budget
            budget = max(0, budget - row["total_remaining"])
            if row["at_risk"]:
                summary["at_risk_count"] += 1
    else:
        for row in rows:
            row["at_risk"] = False
    return {"rows": rows, "summary": summary}


def workload_snapshot(user) -> dict:
    return workload_rows(user)["summary"]


def chapter_workload(user, chapter) -> dict:
    links = {link.syllabus_subject_id: link for link in user.subjects}
    override = StudentChapterPlan.query.filter_by(
        user_id=user.id, syllabus_chapter_id=chapter.id
    ).first()
    subject = chapter.subject
    link = links.get(subject.id) if subject else None
    base = base_stage_minutes(chapter)
    personalized = personalized_stage_minutes(
        chapter,
        subject_confidence=link.confidence if link else 3,
        override=override,
    )
    return {
        "base": base,
        "personalized": personalized,
        "base_total": sum(base.values()),
        "override": override,
        "confidence_label": CONFIDENCE_LABELS[personalized["confidence"]],
    }
