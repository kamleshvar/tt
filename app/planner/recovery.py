from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from app.models import OPEN_TASK_STATUSES, StudyPlan, StudyTask, TaskEvent
from app.planner.progress import refresh_overdue_tasks
from app.planner.scheduler import find_slot, get_study_slots
from extensions import db

RECOVERY_DAILY_LIMIT = 120
RECOVERY_TASK_LIMIT = 2


class RecoveryError(Exception):
    pass


def _horizon(user) -> tuple[date, date]:
    today = date.today()
    exam = user.profile.exam_date if user.profile else None
    if exam and exam < today:
        raise RecoveryError("The recorded examination date has passed. Update the exam date first.")
    return today, exam or (today + timedelta(days=30))


def missed_tasks(user) -> list[StudyTask]:
    refresh_overdue_tasks(user.id)
    return (
        StudyTask.query.filter(
            StudyTask.user_id == user.id,
            StudyTask.status.in_(["overdue", "pending", "in_progress", "postponed"]),
            StudyTask.scheduled_date < date.today(),
        )
        .order_by(StudyTask.priority.asc(), StudyTask.scheduled_date.asc(), StudyTask.id.asc())
        .all()
    )


def _day_capacity(user, start: date, end: date) -> dict[date, int]:
    capacities = {}
    current = start
    cap_limit = min(user.preferences.daily_cap_minutes if user.preferences else 120, RECOVERY_DAILY_LIMIT)
    while current <= end:
        slots = get_study_slots(user, current)
        capacities[current] = min(cap_limit, sum(slot.minutes for slot in slots))
        current += timedelta(days=1)
    return capacities


def preview_recovery(user) -> dict:
    tasks = missed_tasks(user)
    if not tasks:
        return {
            "tasks": [],
            "total_minutes": 0,
            "available_minutes": 0,
            "feasible": True,
            "warnings": [],
            "proposals": [],
        }
    start, end = _horizon(user)
    capacities = _day_capacity(user, start, end)
    total_available = sum(capacities.values())
    total_remaining = sum(task.estimated_minutes for task in tasks)
    loads = defaultdict(int)
    counts = defaultdict(int)
    proposals = []
    unplaced = []
    candidates = sorted(capacities.keys())
    for task in tasks:
        chosen = None
        for day in candidates:
            if loads[day] + task.estimated_minutes > capacities[day]:
                continue
            if counts[day] >= RECOVERY_TASK_LIMIT:
                continue
            chosen = day
            break
        if chosen is None:
            unplaced.append(task)
            continue
        loads[chosen] += task.estimated_minutes
        counts[chosen] += 1
        proposals.append(
            {
                "task": task,
                "date": chosen,
                "remaining_day_capacity": capacities[chosen] - loads[chosen],
            }
        )
    warnings = []
    if total_remaining > total_available:
        warnings.append(
            f"The missed work needs {total_remaining} minutes, but only "
            f"{total_available} minutes of safe recovery capacity remain before "
            f"{end.strftime('%d %b %Y')}."
        )
    if unplaced:
        warnings.append(
            f"{len(unplaced)} task(s) cannot be placed without overloading a day or "
            "exceeding the deadline. Those tasks are listed as at risk."
        )
    feasible = not unplaced and total_remaining <= total_available
    return {
        "tasks": tasks,
        "unplaced": unplaced,
        "total_minutes": total_remaining,
        "available_minutes": total_available,
        "feasible": feasible,
        "warnings": warnings,
        "proposals": proposals,
        "horizon_end": end,
    }


def apply_recovery(user) -> dict:
    tasks = missed_tasks(user)
    if not tasks:
        return {"moved": 0, "remaining": 0, "warnings": ["There are no missed tasks to recover."]}
    start, end = _horizon(user)
    capacities = _day_capacity(user, start, end)
    total_available = sum(capacities.values())
    total_remaining = sum(task.estimated_minutes for task in tasks)
    loads = defaultdict(int)
    counts = defaultdict(int)
    moved = 0
    remaining_at_risk = []
    candidates = sorted(capacities.keys())
    try:
        for task in tasks:
            old_date = task.scheduled_date
            chosen = None
            for day in candidates:
                if loads[day] + task.estimated_minutes > capacities[day]:
                    continue
                if counts[day] >= RECOVERY_TASK_LIMIT:
                    continue
                slot = find_slot(
                    user,
                    task.estimated_minutes,
                    day,
                    day,
                    exclude_task_id=task.id,
                )
                if slot is None:
                    continue
                chosen = (day, slot)
                break
            if chosen is None:
                remaining_at_risk.append(task)
                continue
            day, slot = chosen
            task.scheduled_date = slot.day
            task.start_time = slot.start_time
            task.end_time = slot.end_time
            task.status = "pending"
            loads[day] += task.estimated_minutes
            counts[day] += 1
            moved += 1
            db.session.add(
                TaskEvent(
                    task=task,
                    user_id=user.id,
                    event_type="recovery_rescheduled",
                    from_status="overdue",
                    to_status="pending",
                    old_date=old_date,
                    new_date=day,
                    note="Moved by the missed-work recovery plan.",
                )
            )
            db.session.flush()
        warnings = []
        if total_remaining > total_available:
            warnings.append(
                "Recovery capacity is below the amount of missed work. "
                "Prioritise high-weightage chapters and consider shortening sessions."
            )
        if remaining_at_risk:
            warnings.append(
                f"{len(remaining_at_risk)} task(s) remain overdue because no safe slot could be found."
            )
        if moved:
            warnings.append(
                "Recovery spread tasks across available days instead of stacking them onto tomorrow."
            )
        plan = (
            StudyPlan.query.filter_by(user_id=user.id, status="active")
            .order_by(StudyPlan.version_number.desc())
            .first()
        )
        if plan is not None:
            existing = list(plan.warnings or [])
            for warning in warnings:
                marker = f"Recovery: {warning}"
                if marker not in existing:
                    existing.append(marker)
            plan.warnings = existing
            plan.feasible = plan.feasible and not remaining_at_risk
        db.session.commit()
        return {
            "moved": moved,
            "remaining": len(remaining_at_risk),
            "warnings": warnings,
            "available_minutes": total_available,
            "total_minutes": total_remaining,
        }
    except Exception:
        db.session.rollback()
        raise
