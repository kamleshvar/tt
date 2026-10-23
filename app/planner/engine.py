from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.models import (
    MockTest,
    OPEN_TASK_STATUSES,
    RevisionRecord,
    StudentChapterPlan,
    StudyPlan,
    StudyTask,
    TaskEvent,
    utcnow,
)
from app.planner.progress import selected_chapters
from app.planner.scheduler import SchedulePool, validate_time_settings
from app.planner.workload import personalized_stage_minutes
from extensions import db


class PlanGenerationError(Exception):
    pass


@dataclass
class ChapterPlan:
    chapter: object
    subject: object
    confidence: int
    priority: int
    learning_required: int
    practice_required: int
    revision_required: int
    completed_learning: int
    completed_practice: int
    completed_revision: dict
    completed_learning_date: date | None = None
    completed_practice_date: date | None = None
    last_learning_date: date | None = None
    last_practice_date: date | None = None
    last_revision_date: date | None = None
    revision_slots: dict = field(default_factory=dict)

    @property
    def learning_remaining(self) -> int:
        return max(0, self.learning_required - self.completed_learning)

    @property
    def practice_remaining(self) -> int:
        return max(0, self.practice_required - self.completed_practice)


@dataclass
class Template:
    chapter_plan: ChapterPlan
    task_type: str
    title: str
    duration: int
    earliest: date
    priority: int
    topic_id: int | None = None
    revision_cycle: int = 0


def _split_minutes(total: int, session: int) -> list[int]:
    total = max(1, int(total))
    session = max(20, int(session))
    count = max(1, math.ceil(total / session))
    base, extra = divmod(total, count)
    chunks = [base + (1 if index < extra else 0) for index in range(count)]
    return [max(1, value) for value in chunks]


def _part_suffix(index: int, total: int) -> str:
    return f" (part {index}/{total})" if total > 1 else ""


def _learning_templates(
    cp: ChapterPlan, session: int, done_learning: int
) -> list[Template]:
    remaining = cp.learning_remaining
    if remaining <= 0:
        return []
    chapter = cp.chapter
    subject = cp.subject
    base = f"{subject.name} · {chapter.name}"
    templates: list[Template] = []
    topics = [topic for topic in chapter.topics if not topic.is_removed]
    if topics and done_learning == 0 and remaining >= len(topics) * 20:
        raw_weights = [
            max(1, int(topic.estimated_minutes or remaining / len(topics)))
            for topic in topics
        ]
        weight_sum = sum(raw_weights)
        allocations = [
            max(20, round(remaining * weight / weight_sum)) for weight in raw_weights
        ]
        diff = remaining - sum(allocations)
        allocations[-1] = max(1, allocations[-1] + diff)
        for topic, allocation in zip(topics, allocations):
            chunks = _split_minutes(allocation, session)
            for index, duration in enumerate(chunks, start=1):
                templates.append(
                    Template(
                        chapter_plan=cp,
                        task_type="learning",
                        title=f"Learn: {topic.name} — {chapter.name} · {subject.name}"
                        + _part_suffix(index, len(chunks)),
                        duration=duration,
                        earliest=date.today(),
                        priority=cp.priority,
                        topic_id=topic.id,
                    )
                )
    else:
        chunks = _split_minutes(remaining, session)
        for index, duration in enumerate(chunks, start=1):
            templates.append(
                Template(
                    chapter_plan=cp,
                    task_type="learning",
                    title=f"Learn: {base}" + _part_suffix(index, len(chunks)),
                    duration=duration,
                    earliest=date.today(),
                    priority=cp.priority,
                    topic_id=None,
                )
            )
    return templates


def _practice_templates(cp: ChapterPlan, session: int) -> list[Template]:
    remaining = cp.practice_remaining
    if remaining <= 0:
        return []
    earliest_source = cp.last_learning_date or cp.completed_learning_date
    earliest = (earliest_source + timedelta(days=2)) if earliest_source else date.today()
    if earliest < date.today():
        earliest = date.today()
    chunks = _split_minutes(remaining, session)
    subject = cp.subject
    chapter = cp.chapter
    return [
        Template(
            chapter_plan=cp,
            task_type="practice",
            title=f"Practice: {subject.name} · {chapter.name}"
            + _part_suffix(index, len(chunks)),
            duration=duration,
            earliest=earliest,
            priority=cp.priority + 3,
        )
        for index, duration in enumerate(chunks, start=1)
    ]


def _revision_templates(cp: ChapterPlan, session: int, frequency: int) -> list[Template]:
    templates = []
    previous = cp.last_practice_date or cp.completed_practice_date or date.today()
    subject = cp.subject
    chapter = cp.chapter
    gap_by_cycle = {1: 5, 2: 12, 3: 21}
    frequency = max(1, int(frequency or 1))
    base_target, extra = divmod(max(0, cp.revision_required), frequency)
    for cycle in range(1, frequency + 1):
        record = cp.revision_slots.get(cycle)
        if record is not None and record.status == "completed":
            previous = record.completed_date or record.scheduled_date or previous
            continue
        completed = cp.completed_revision.get(cycle, 0)
        cycle_target = base_target + (1 if cycle <= extra else 0)
        remaining = max(0, cycle_target - completed)
        if remaining <= 0:
            previous = previous + timedelta(days=gap_by_cycle.get(cycle, 12))
            continue
        earliest = previous + timedelta(days=gap_by_cycle.get(cycle, 12))
        if earliest < date.today():
            earliest = date.today()
        chunks = _split_minutes(remaining, session)
        for index, duration in enumerate(chunks, start=1):
            templates.append(
                Template(
                    chapter_plan=cp,
                    task_type="revision",
                    title=f"Revision {cycle}: {subject.name} · {chapter.name}"
                    + _part_suffix(index, len(chunks)),
                    duration=duration,
                    earliest=earliest,
                    priority=cp.priority + 6,
                    revision_cycle=cycle,
                )
            )
        previous = earliest
    return templates


def _make_task(user, plan, template: Template, slot) -> StudyTask:
    task = StudyTask(
        user_id=user.id,
        plan_id=plan.id,
        syllabus_chapter_id=template.chapter_plan.chapter.id,
        syllabus_topic_id=template.topic_id,
        task_type=template.task_type,
        title=template.title[:250],
        original_scheduled_date=slot.day,
        scheduled_date=slot.day,
        start_time=slot.start_time,
        end_time=slot.end_time,
        estimated_minutes=template.duration,
        status="pending",
        priority=template.priority,
        revision_cycle=template.revision_cycle,
    )
    db.session.add(task)
    db.session.flush()
    return task


def _schedule_mocks(user, plan, pool: SchedulePool, horizon_end: date) -> tuple[list[StudyTask], list[str]]:
    tasks = []
    warnings = []
    exam = user.profile.exam_date if user.profile else None
    targets = []
    if exam:
        for offset in (21, 14, 7):
            target = exam - timedelta(days=offset)
            if target > date.today():
                targets.append(target)
    else:
        target = max(date.today() + timedelta(days=7), horizon_end - timedelta(days=14))
        targets.append(min(target, horizon_end))
    if not targets:
        warnings.append(
            "No mock examination could be scheduled because the deadline is too close "
            "to reserve a full-syllabus session. Add one manually from Mock Tests."
        )
    used_dates = set()
    for index, target in enumerate(targets[:3], start=1):
        slot = pool.reserve_contiguous(
            120,
            max(date.today(), target - timedelta(days=3)),
            min(horizon_end, target + timedelta(days=3)),
        )
        if slot is None:
            slot = pool.reserve_contiguous(
                120, date.today(), horizon_end
            )
        if slot is not None and slot.day in used_dates:
            alt = pool.reserve_contiguous(
                90, slot.day + timedelta(days=1), horizon_end
            )
            slot = alt
        if slot is None:
            warnings.append(
                f"Mock examination {index} could not be fitted into a conflict-free "
                "block before the deadline. Add one manually from Mock Tests."
            )
            continue
        if slot.day in used_dates:
            warnings.append(
                f"Mock examination {index} could not be given its own day and was skipped."
            )
            continue
        used_dates.add(slot.day)
        duration = slot.minutes
        task = StudyTask(
            user_id=user.id,
            plan_id=plan.id,
            task_type="mock",
            title=f"Full-syllabus mock examination {index}",
            original_scheduled_date=slot.day,
            scheduled_date=slot.day,
            start_time=slot.start_time,
            end_time=slot.end_time,
            estimated_minutes=duration,
            status="pending",
            priority=5,
        )
        db.session.add(task)
        db.session.flush()
        db.session.add(
            MockTest(
                user_id=user.id,
                task_id=task.id,
                title=f"Full-syllabus mock examination {index}",
                scheduled_date=slot.day,
                duration_minutes=duration,
                status="pending",
                notes="Generated by the study planner. Score can be recorded on the Mock Tests page.",
            )
        )
        tasks.append(task)
    return tasks, warnings


def generate_study_plan(user, *, regenerate: bool = False, now=None) -> StudyPlan:
    profile = user.profile
    preferences = user.preferences
    if profile is None or preferences is None:
        raise PlanGenerationError("Complete your student profile before generating a plan.")
    if not profile.onboarding_completed:
        raise PlanGenerationError("Complete onboarding before generating a study plan.")
    time_errors = validate_time_settings(
        wake=preferences.wake_time,
        sleep=preferences.sleep_time,
        preferred_start=preferences.preferred_study_start,
        preferred_end=preferences.preferred_study_end,
        session_minutes=preferences.session_duration_minutes,
        break_minutes=preferences.break_duration_minutes,
        daily_cap=preferences.daily_cap_minutes,
        revision_frequency=preferences.revision_frequency,
        lighter_days=preferences.lighter_days or [],
    )
    if time_errors:
        raise PlanGenerationError(" ".join(time_errors))

    links = [
        link
        for link in user.subjects
        if link.is_active and link.syllabus_subject and not link.syllabus_subject.is_removed
    ]
    if not links:
        raise PlanGenerationError("Select at least one subject during onboarding first.")
    versions = {
        link.syllabus_subject.version
        for link in links
        if link.syllabus_subject.version is not None
    }
    if not versions:
        raise PlanGenerationError("Your selected subjects are not linked to a syllabus version.")
    if len(versions) > 1:
        raise PlanGenerationError(
            "Your subjects point to different syllabus versions. Ask the administrator to "
            "align them before planning."
        )
    version = versions.pop()

    today = date.today()
    exam = profile.exam_date
    if exam and exam < today:
        raise PlanGenerationError("The board examination date has passed. Update it first.")
    horizon_end = exam or (today + timedelta(days=180))

    active = (
        StudyPlan.query.filter_by(user_id=user.id, status="active")
        .order_by(StudyPlan.version_number.desc())
        .first()
    )
    if active is not None and not regenerate:
        return active

    completed_preserved = 0
    if active is not None:
        active.status = "superseded"
        for old_task in active.tasks:
            if old_task.status in OPEN_TASK_STATUSES:
                previous = old_task.status
                old_task.status = "cancelled"
                linked_mock = MockTest.query.filter_by(
                    user_id=user.id, task_id=old_task.id, status="pending"
                ).first()
                if linked_mock is not None:
                    linked_mock.status = "cancelled"
                db.session.add(
                    TaskEvent(
                        task=old_task,
                        user_id=user.id,
                        event_type="cancelled_by_regeneration",
                        from_status=previous,
                        to_status="cancelled",
                        old_date=old_task.scheduled_date,
                        new_date=None,
                        note="Open task replaced by a newly generated plan. Completed work was preserved.",
                    )
                )
            elif old_task.status == "completed":
                completed_preserved += 1
        db.session.flush()

    max_version = (
        db.session.query(db.func.max(StudyPlan.version_number))
        .filter(StudyPlan.user_id == user.id)
        .scalar()
        or 0
    )
    plan = StudyPlan(
        user_id=user.id,
        syllabus_version_id=version.id,
        status="active",
        version_number=max_version + 1,
        feasible=True,
        warnings=[],
        parameters={},
        horizon_start=today,
        horizon_end=horizon_end,
        generated_at=utcnow(),
    )
    db.session.add(plan)
    db.session.flush()

    all_completed = (
        StudyTask.query.filter_by(user_id=user.id, status="completed").all()
    )
    completed_learning = {}
    completed_practice = {}
    completed_revision = {}
    completed_learning_date = {}
    completed_practice_date = {}
    for task in all_completed:
        if not task.syllabus_chapter_id:
            continue
        minutes = task.actual_minutes or task.estimated_minutes
        if task.task_type == "learning":
            completed_learning[task.syllabus_chapter_id] = (
                completed_learning.get(task.syllabus_chapter_id, 0) + minutes
            )
            completed_learning_date[task.syllabus_chapter_id] = max(
                completed_learning_date.get(task.syllabus_chapter_id, date.min),
                task.scheduled_date,
            )
        elif task.task_type == "practice":
            completed_practice[task.syllabus_chapter_id] = (
                completed_practice.get(task.syllabus_chapter_id, 0) + minutes
            )
            completed_practice_date[task.syllabus_chapter_id] = max(
                completed_practice_date.get(task.syllabus_chapter_id, date.min),
                task.scheduled_date,
            )
        elif task.task_type == "revision" and task.syllabus_chapter_id:
            key = (task.syllabus_chapter_id, task.revision_cycle or 1)
            completed_revision[key] = completed_revision.get(key, 0) + minutes

    records = {
        (record.syllabus_chapter_id, record.revision_number): record
        for record in RevisionRecord.query.filter_by(user_id=user.id).all()
        if record.syllabus_chapter_id
    }

    overrides = {
        item.syllabus_chapter_id: item
        for item in StudentChapterPlan.query.filter_by(user_id=user.id).all()
    }
    chapter_plans: list[ChapterPlan] = []
    seen_chapters = set()
    for link in links:
        subject = link.syllabus_subject
        subject_confidence = max(1, min(5, link.confidence or 3))
        for unit in subject.units:
            for chapter in sorted(unit.chapters, key=lambda item: item.order_index):
                if chapter.is_removed or chapter.id in seen_chapters:
                    continue
                seen_chapters.add(chapter.id)
                override = overrides.get(chapter.id)
                workload = personalized_stage_minutes(
                    chapter,
                    subject_confidence=subject_confidence,
                    override=override,
                )
                confidence = workload["confidence"]
                weight = max(0, float(chapter.weightage_marks or 0))
                priority = max(
                    8, int(70 - min(weight, 12) * 4 - (6 - confidence) * 4)
                )
                learning_required = workload["learning"]
                practice_required = workload["practice"]
                revision_required = workload["revision"]
                cp = ChapterPlan(
                    chapter=chapter,
                    subject=subject,
                    confidence=confidence,
                    priority=priority,
                    learning_required=learning_required,
                    practice_required=practice_required,
                    revision_required=revision_required,
                    completed_learning=completed_learning.get(chapter.id, 0),
                    completed_practice=completed_practice.get(chapter.id, 0),
                    completed_revision={
                        cycle: minutes
                        for (chapter_id, cycle), minutes in completed_revision.items()
                        if chapter_id == chapter.id
                    },
                    completed_learning_date=(
                        completed_learning_date.get(chapter.id)
                        if chapter.id in completed_learning_date
                        else None
                    ),
                    completed_practice_date=(
                        completed_practice_date.get(chapter.id)
                        if chapter.id in completed_practice_date
                        else None
                    ),
                    revision_slots={
                        cycle: record
                        for (chapter_id, cycle), record in records.items()
                        if chapter_id == chapter.id
                    },
                )
                chapter_plans.append(cp)
    if not chapter_plans:
        raise PlanGenerationError("No active syllabus chapters are assigned to your subjects.")

    chapter_plans.sort(
        key=lambda cp: (-float(cp.chapter.weightage_marks or 0), cp.confidence, cp.chapter.order_index)
    )

    pool = SchedulePool(user, today, horizon_end, now=now)
    mocks, mock_warnings = _schedule_mocks(user, plan, pool, horizon_end)
    pool.prepare_slots()
    available_after_mocks = pool.capacity_minutes()

    unplaced: list[str] = []
    subject_warnings = list(mock_warnings)
    session = preferences.session_duration_minutes

    learning_templates = []
    for cp in chapter_plans:
        learning_templates.extend(
            _learning_templates(cp, session, cp.completed_learning)
        )
    learning_templates.sort(key=lambda t: (t.priority, t.chapter_plan.chapter.order_index))
    for template in learning_templates:
        slot = pool.schedule(template.duration, template.earliest)
        if slot is None:
            unplaced.append(template.title)
            continue
        _make_task(user, plan, template, slot)
        cp = template.chapter_plan
        cp.last_learning_date = max(cp.last_learning_date or slot.day, slot.day)

    practice_templates = []
    for cp in chapter_plans:
        practice_templates.extend(_practice_templates(cp, session))
    practice_templates.sort(key=lambda t: (t.priority, t.earliest))
    for template in practice_templates:
        slot = pool.schedule(template.duration, template.earliest)
        if slot is None:
            unplaced.append(template.title)
            continue
        _make_task(user, plan, template, slot)
        cp = template.chapter_plan
        cp.last_practice_date = max(cp.last_practice_date or slot.day, slot.day)

    revision_templates = []
    for cp in chapter_plans:
        revision_templates.extend(
            _revision_templates(cp, session, preferences.revision_frequency or 2)
        )
    revision_templates.sort(key=lambda t: (t.priority, t.earliest, t.revision_cycle))
    revision_records_created = set()
    for template in revision_templates:
        slot = pool.schedule(template.duration, template.earliest)
        if slot is None:
            unplaced.append(template.title)
            continue
        _make_task(user, plan, template, slot)
        cp = template.chapter_plan
        key = (cp.chapter.id, template.revision_cycle)
        if key not in revision_records_created:
            record = cp.revision_slots.get(template.revision_cycle)
            if record is None:
                record = RevisionRecord(
                    user_id=user.id,
                    syllabus_chapter_id=cp.chapter.id,
                    revision_number=template.revision_cycle,
                    scheduled_date=slot.day,
                    status="pending",
                )
                db.session.add(record)
                cp.revision_slots[template.revision_cycle] = record
            else:
                record.scheduled_date = slot.day
                record.status = "pending"
            revision_records_created.add(key)

    exam = profile.exam_date
    buffer_targets = []
    if horizon_end > today + timedelta(days=6):
        days_until_sunday = (6 - today.weekday()) % 7
        first_sunday = today + timedelta(days=days_until_sunday)
        for offset in range(0, min(8, max(1, (horizon_end - first_sunday).days // 7 + 1))):
            target = first_sunday + timedelta(days=offset * 7)
            if target <= horizon_end:
                buffer_targets.append(target)
    for target in buffer_targets:
        slot = pool.schedule(30, target)
        if slot is None or slot.day != target:
            continue
        task = StudyTask(
            user_id=user.id,
            plan_id=plan.id,
            task_type="buffer",
            title="Weekly catch-up buffer",
            original_scheduled_date=slot.day,
            scheduled_date=slot.day,
            start_time=slot.start_time,
            end_time=slot.end_time,
            estimated_minutes=30,
            status="pending",
            priority=90,
            notes="Reserved recovery time. Use it for a missed task or rest; do not treat it as syllabus coverage.",
        )
        db.session.add(task)

    required_remaining = sum(t.duration for t in learning_templates)
    required_remaining += sum(t.duration for t in practice_templates)
    required_remaining += sum(t.duration for t in revision_templates)
    capacity_risk = required_remaining > available_after_mocks
    warnings: list[str] = list(subject_warnings)
    if capacity_risk:
        warnings.append(
            f"The remaining plan needs about {required_remaining} minutes of study, but the "
            f"conflict-free capacity before {horizon_end.strftime('%d %b %Y')} is about "
            f"{available_after_mocks} minutes. The plan prioritises high-weightage and "
            "lower-confidence chapters and reports anything it cannot fit."
        )
    if unplaced:
        preview = ", ".join(unplaced[:5])
        more = f" and {len(unplaced) - 5} more" if len(unplaced) > 5 else ""
        warnings.append(
            f"{len(unplaced)} task(s) could not be placed before the deadline: {preview}{more}. "
            "They were not silently dropped. Reduce workload, increase available time, or move "
            "the examination date if it is incorrect."
        )
    else:
        warnings.append(
            "Every generated task fits before the current deadline. This is a timetable, not a "
            "guarantee of examination readiness."
        )
    if not profile.exam_date:
        warnings.append(
            "No board examination date is set. A 180-day horizon was used; add the real date "
            "and regenerate for accurate deadline planning."
        )
    if version.status != "validated":
        warnings.append(
            f"The syllabus version “{version.version_label}” is {version.status_label.lower()}. "
            "Its chapter list and estimates must be verified by an administrator."
        )
    if capacity_risk and profile.exam_date:
        warnings.append(
            "Workload risk: not all desired revision and practice can comfortably fit before the "
            "board examination. Review the unplaced tasks and prioritise."
        )
    if active is not None and completed_preserved:
        warnings.append(
            f"{completed_preserved} completed task(s) were preserved from the previous plan. "
            "Remaining work was calculated from completed minutes to avoid duplicates."
        )

    plan.feasible = not unplaced and not capacity_risk
    plan.warnings = warnings
    chapter_coverage = sum(
        1
        for cp in chapter_plans
        if cp.completed_learning > 0 or cp.learning_remaining == 0 or cp.last_learning_date
    )
    plan.parameters = {
        "available_minutes": available_after_mocks,
        "required_minutes": required_remaining,
        "unplaced": unplaced,
        "unplaced_count": len(unplaced),
        "learning_templates": len(learning_templates),
        "practice_templates": len(practice_templates),
        "revision_templates": len(revision_templates),
        "mock_sessions": len(mocks),
        "completed_preserved": completed_preserved,
        "chapters_selected": len(chapter_plans),
        "chapters_with_learning": chapter_coverage,
        "exam_date_known": bool(profile.exam_date),
        "syllabus_version": version.version_label,
        "session_minutes": preferences.session_duration_minutes,
        "revision_frequency": preferences.revision_frequency,
        "personalized_workload_minutes": sum(
            cp.learning_required + cp.practice_required + cp.revision_required
            for cp in chapter_plans
        ),
        "revision_only_chapters": sum(
            1
            for cp in chapter_plans
            if overrides.get(cp.chapter.id)
            and overrides[cp.chapter.id].study_mode == "revision_only"
        ),
    }
    plan.parameters = {k: v for k, v in plan.parameters.items() if k != "unplaced"} | {
        "unplaced": unplaced
    }
    profile.plan_generated_at = utcnow()
    db.session.commit()
    return plan
