from __future__ import annotations

import calendar as calendar_module
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.auth.forms import ProfileForm, SecureForm
from app.models import (
    ExamDate,
    MockTest,
    OPEN_TASK_STATUSES,
    RevisionRecord,
    StudentChapterPlan,
    StudentSubject,
    StudyPlan,
    StudyTask,
    SyllabusSubject,
    WeeklyCommitment,
)
from app.planner.engine import PlanGenerationError, generate_study_plan
from app.planner.progress import (
    chapter_progress_rows,
    progress_snapshot,
    refresh_overdue_tasks,
    selected_chapters,
    task_stats,
    weekly_summary,
)
from app.planner.workload import (
    chapter_workload,
    workload_rows,
    workload_snapshot,
)
from app.planner.recovery import RecoveryError, apply_recovery, preview_recovery
from app.planner.scheduler import (
    commitments_for_day,
    get_study_slots,
    minutes_to_time,
    parse_clock,
    sleep_minutes,
    time_to_minutes,
    validate_time_settings,
)
from app.security import is_safe_redirect_target
from app.services.accounts import record_student_activity
from app.services.task_management import (
    TaskTransitionError,
    postpone_task,
    set_task_status,
    split_task,
)
from app.student import student_bp
from app.student.forms import MockTestForm, ScoreForm
from app.syllabus.loader import current_syllabus_version
from extensions import db

BOARD_VALUES = {"CBSE", "CISCE", "State Board", "Other"}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
YEAR_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def requires_onboarding(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.onboarding_complete:
            flash("Finish the short setup before opening your planner.", "info")
            return redirect(url_for("student.onboarding"))
        return view(*args, **kwargs)

    return wrapped


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _safe_return(default_url: str) -> str:
    target = request.form.get("next")
    if target and is_safe_redirect_target(target, request.host_url):
        return target
    return default_url


def _int_field(name: str, minimum: int, maximum: int, label: str) -> int:
    raw = request.form.get(name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if value < minimum or value > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return value


def _travel_field(name: str) -> int:
    raw = request.form.get(name, "0") or "0"
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("Travel buffers must be whole minutes.")
    if value < 0 or value > 180:
        raise ValueError("Travel buffers must be between 0 and 180 minutes.")
    return value


def _expanded(day, start, end, travel_before, travel_after):
    return (
        day,
        max(0, time_to_minutes(start) - travel_before),
        min(24 * 60, time_to_minutes(end) + travel_after),
    )


def _validate_commitment_entries(entries: list[dict]) -> list[str]:
    grouped = defaultdict(list)
    for entry in entries:
        grouped[entry["day"]].append(entry)
    errors = []
    for day, items in grouped.items():
        ordered = sorted(items, key=lambda item: item["start"])
        for index in range(1, len(ordered)):
            previous = ordered[index - 1]
            current = ordered[index]
            if current["start"] < previous["end"]:
                errors.append(
                    f"{DAY_NAMES[day]} has overlapping commitments: "
                    f"“{previous['title']}” and “{current['title']}”."
                )
    return errors


def _current_version_for_student(profile=None):
    profile = profile or current_user.profile
    return current_syllabus_version(profile.board, profile.academic_year)


@student_bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    profile = current_user.profile
    if profile is None:
        abort(403)
    if profile.onboarding_completed and request.method == "GET":
        return redirect(url_for("student.dashboard"))
    form = SecureForm()
    step = request.args.get("step", type=int) or profile.onboarding_step or 1
    step = max(1, min(4, step))
    if request.method == "POST" and form.validate_on_submit():
        action = request.form.get("action", "next")
        if action == "back":
            profile.onboarding_step = max(1, step - 1)
            db.session.commit()
            return redirect(url_for("student.onboarding", step=profile.onboarding_step))
        try:
            if step == 1:
                _save_academic_step(profile)
            elif step == 2:
                _save_subjects_step(profile)
            elif step == 3:
                _save_schedule_step(profile)
            elif step == 4:
                _finish_preferences_step(profile)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(
                "student/onboarding.html",
                form=form,
                step=step,
                **_onboarding_context(profile, step),
            ), 400
        if step < 4:
            profile.onboarding_step = step + 1
            db.session.commit()
            flash("Progress saved. You can return to this step later.", "success")
            return redirect(url_for("student.onboarding", step=profile.onboarding_step))
        flash("Setup complete. Your plan is ready to generate.", "success")
        try:
            generate_study_plan(current_user)
            flash("Your first study plan has been generated.", "success")
        except PlanGenerationError as exc:
            db.session.rollback()
            flash(f"Setup saved, but the plan needs attention: {exc}", "warning")
        return redirect(url_for("student.dashboard"))
    if request.method == "POST":
        flash("The form expired or CSRF validation failed. Refresh and try again.", "error")
    return render_template(
        "student/onboarding.html",
        form=form,
        step=step,
        **_onboarding_context(profile, step),
    )


def _onboarding_context(profile, step):
    context = {"profile": profile, "days": DAY_NAMES}
    if step == 2:
        version = _current_version_for_student(profile)
        links = {
            link.syllabus_subject_id: link for link in current_user.subjects
        }
        context.update(version=version, links=links)
    if step == 3:
        school = {}
        tuition = {}
        others = []
        for item in current_user.commitments:
            if not item.is_active:
                continue
            if item.commitment_type == "school":
                school[item.day_of_week] = item
            elif item.commitment_type == "tuition":
                tuition[item.day_of_week] = item
            else:
                others.append(item)
        sample = next(iter(school.values()), None) or next(iter(tuition.values()), None)
        context.update(
            school=school,
            tuition=tuition,
            others=others,
            sample_travel_before=sample.travel_before_minutes if sample else 30,
            sample_travel_after=sample.travel_after_minutes if sample else 15,
        )
    if step == 4:
        context.update(preferences=current_user.preferences)
    return context


def _save_academic_step(profile):
    board = (request.form.get("board") or "").strip()
    if board not in BOARD_VALUES:
        raise ValueError("Choose a valid Class 12 board.")
    academic_year = (request.form.get("academic_year") or "").strip()
    if not YEAR_PATTERN.match(academic_year):
        raise ValueError("Academic year must look like 2026-27.")
    exam_raw = (request.form.get("exam_date") or "").strip()
    exam_date = _parse_date(exam_raw) if exam_raw else None
    if exam_raw and exam_date is None:
        raise ValueError("Enter the board examination date in a valid format.")
    if exam_date and exam_date < date.today():
        raise ValueError("The board examination date cannot be in the past.")
    profile.board = board
    profile.academic_year = academic_year
    profile.exam_date = exam_date
    primary = ExamDate.query.filter_by(user_id=current_user.id, is_primary=True).first()
    if exam_date:
        if primary is None:
            primary = ExamDate(
                user_id=current_user.id,
                title="Board Examination",
                exam_type="board",
                is_primary=True,
            )
            db.session.add(primary)
        primary.exam_date = exam_date
        primary.title = "Board Examination"
        primary.exam_type = "board"
    elif primary is not None:
        db.session.delete(primary)
    db.session.commit()


def _save_subjects_step(profile):
    version = current_syllabus_version(profile.board, profile.academic_year)
    if version is None:
        raise ValueError(
            "No syllabus is configured for this board and academic year yet. "
            "Please contact the administrator before continuing."
        )
    valid_subjects = {
        subject.id: subject for subject in version.subjects if not subject.is_removed
    }
    try:
        selected_ids = {
            int(value) for value in request.form.getlist("subjects") if value
        }
    except ValueError:
        raise ValueError("The subject selection was invalid.")
    if not selected_ids:
        raise ValueError("Select at least one subject.")
    if not selected_ids.issubset(valid_subjects.keys()):
        raise ValueError("One or more selected subjects are not in the current syllabus.")
    selected_ids = list(selected_ids)
    existing = {
        link.syllabus_subject_id: link for link in current_user.subjects
    }
    for subject_id, link in existing.items():
        if subject_id not in selected_ids:
            db.session.delete(link)
    for subject_id in selected_ids:
        confidence_raw = request.form.get(f"confidence_{subject_id}", "3")
        try:
            confidence = int(confidence_raw)
        except ValueError:
            confidence = 3
        confidence = max(1, min(5, confidence))
        performance = (request.form.get(f"performance_{subject_id}") or "").strip()[:40]
        link = existing.get(subject_id)
        if link is None:
            link = StudentSubject(
                user_id=current_user.id,
                syllabus_subject_id=subject_id,
                is_active=True,
            )
            db.session.add(link)
        link.is_active = True
        link.confidence = confidence
        link.recent_performance = performance
    db.session.commit()


def _save_schedule_step(profile):
    wake = parse_clock(request.form.get("wake_time"))
    sleep = parse_clock(request.form.get("sleep_time"))
    if wake is None or sleep is None:
        raise ValueError("Enter valid wake and sleep times.")
    if wake == sleep:
        raise ValueError("Wake and sleep times cannot be identical.")
    if sleep_minutes(wake, sleep) < 300:
        raise ValueError("Your schedule must allow at least 5 hours of sleep.")
    school_before = _travel_field("school_travel_before")
    school_after = _travel_field("school_travel_after")
    tuition_before = _travel_field("tuition_travel_before")
    tuition_after = _travel_field("tuition_travel_after")
    parsed = []
    for day in range(7):
        for prefix, title, kind, before, after in (
            ("school", "School", "school", school_before, school_after),
            ("tuition", "Tuition", "tuition", tuition_before, tuition_after),
        ):
            start_raw = (request.form.get(f"{prefix}_start_{day}") or "").strip()
            end_raw = (request.form.get(f"{prefix}_end_{day}") or "").strip()
            if not start_raw and not end_raw:
                continue
            if not start_raw or not end_raw:
                raise ValueError(f"Enter both start and end times for {title} on {DAY_NAMES[day]}.")
            start = parse_clock(start_raw)
            end = parse_clock(end_raw)
            if start is None or end is None:
                raise ValueError(f"Enter valid times for {title} on {DAY_NAMES[day]}.")
            if end <= start:
                raise ValueError(f"{title} on {DAY_NAMES[day]} must end after it starts.")
            parsed.append(
                {
                    "day": day,
                    "title": title,
                    "kind": kind,
                    "start": time_to_minutes(start),
                    "end": time_to_minutes(end),
                    "start_time": start,
                    "end_time": end,
                    "before": before,
                    "after": after,
                }
            )
    other_title = (request.form.get("other_title") or "").strip()
    if other_title:
        other_day_raw = request.form.get("other_day", "0")
        try:
            other_day = int(other_day_raw)
        except ValueError:
            raise ValueError("Choose a valid day for the other commitment.")
        if other_day < 0 or other_day > 6:
            raise ValueError("Choose a valid day for the other commitment.")
        other_start = parse_clock(request.form.get("other_start"))
        other_end = parse_clock(request.form.get("other_end"))
        if other_start is None or other_end is None or other_end <= other_start:
            raise ValueError("Enter a valid start and end for the other fixed commitment.")
        parsed.append(
            {
                "day": other_day,
                "title": other_title[:120],
                "kind": "other",
                "start": time_to_minutes(other_start),
                "end": time_to_minutes(other_end),
                "start_time": other_start,
                "end_time": other_end,
                "before": 0,
                "after": 0,
            }
        )
    existing_others = [
        item
        for item in current_user.commitments
        if item.is_active and item.commitment_type not in {"school", "tuition"}
    ]
    combined = [
        {
            "day": item.day_of_week,
            "title": item.title,
            "start": time_to_minutes(item.start_time) - item.travel_before_minutes,
            "end": time_to_minutes(item.end_time) + item.travel_after_minutes,
        }
        for item in existing_others
    ] + [
        {
            "day": item["day"],
            "title": item["title"],
            "start": item["start"] - item["before"],
            "end": item["end"] + item["after"],
        }
        for item in parsed
    ]
    overlap_errors = _validate_commitment_entries(combined)
    if overlap_errors:
        raise ValueError(" ".join(overlap_errors))
    WeeklyCommitment.query.filter(
        WeeklyCommitment.user_id == current_user.id,
        WeeklyCommitment.commitment_type.in_(["school", "tuition"]),
    ).delete(synchronize_session=False)
    for item in parsed:
        db.session.add(
            WeeklyCommitment(
                user_id=current_user.id,
                day_of_week=item["day"],
                commitment_type=item["kind"],
                title=item["title"],
                start_time=item["start_time"],
                end_time=item["end_time"],
                travel_before_minutes=item["before"],
                travel_after_minutes=item["after"],
                is_active=True,
            )
        )
    current_user.preferences.wake_time = wake
    current_user.preferences.sleep_time = sleep
    db.session.commit()


def _finish_preferences_step(profile):
    preferences = current_user.preferences
    preferred_start = parse_clock(request.form.get("preferred_study_start"))
    preferred_end = parse_clock(request.form.get("preferred_study_end"))
    if preferred_start is None or preferred_end is None:
        raise ValueError("Enter a valid preferred study period.")
    session = _int_field("session_duration_minutes", 20, 120, "Session duration")
    break_minutes = _int_field("break_duration_minutes", 5, 60, "Break duration")
    break_frequency = _int_field("break_frequency_sessions", 1, 5, "Break frequency")
    daily_cap = _int_field("daily_cap_minutes", 30, 600, "Daily study cap")
    revision_frequency = _int_field("revision_frequency", 1, 3, "Revision frequency")
    try:
        lighter_days = sorted(
            {
                int(value)
                for value in request.form.getlist("lighter_days")
                if value.strip().isdigit() and 0 <= int(value) <= 6
            }
        )
    except ValueError:
        lighter_days = []
    errors = validate_time_settings(
        wake=preferences.wake_time,
        sleep=preferences.sleep_time,
        preferred_start=preferred_start,
        preferred_end=preferred_end,
        session_minutes=session,
        break_minutes=break_minutes,
        daily_cap=daily_cap,
        revision_frequency=revision_frequency,
        lighter_days=lighter_days,
    )
    if errors:
        raise ValueError(" ".join(errors))
    preferences.preferred_study_start = preferred_start
    preferences.preferred_study_end = preferred_end
    preferences.session_duration_minutes = session
    preferences.break_duration_minutes = break_minutes
    preferences.break_frequency_sessions = break_frequency
    preferences.daily_cap_minutes = daily_cap
    preferences.revision_frequency = revision_frequency
    preferences.lighter_days = lighter_days
    preferences.struggle_notes = (request.form.get("struggle_notes") or "").strip()[:2000]
    profile.onboarding_completed = True
    profile.onboarding_step = 4
    db.session.commit()
    record_student_activity(current_user.id, "onboarding_completed", "profile", profile.id)


@student_bp.route("/dashboard")
@requires_onboarding
def dashboard():
    refresh_overdue_tasks(current_user.id)
    today = date.today()
    plan = (
        StudyPlan.query.filter_by(user_id=current_user.id, status="active")
        .order_by(StudyPlan.version_number.desc())
        .first()
    )
    today_tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == current_user.id,
            StudyTask.scheduled_date == today,
            StudyTask.status != "cancelled",
        )
        .order_by(StudyTask.start_time)
        .all()
    )
    today_completed = sum(
        (task.actual_minutes or task.estimated_minutes)
        for task in today_tasks
        if task.status == "completed"
    )
    today_planned = sum(task.estimated_minutes for task in today_tasks)
    snapshot = progress_snapshot(current_user)
    workload = workload_snapshot(current_user)
    week_start = today - timedelta(days=today.weekday())
    week = weekly_summary(current_user.id, week_start)
    upcoming = (
        MockTest.query.filter(
            MockTest.user_id == current_user.id,
            MockTest.status == "pending",
            MockTest.scheduled_date >= today,
        )
        .order_by(MockTest.scheduled_date)
        .limit(4)
        .all()
    )
    deadlines = (
        ExamDate.query.filter(
            ExamDate.user_id == current_user.id, ExamDate.exam_date >= today
        )
        .order_by(ExamDate.exam_date)
        .limit(4)
        .all()
    )
    rows = chapter_progress_rows(current_user)
    subject_groups = defaultdict(list)
    for row in rows:
        subject_groups[row["subject"].name].append(row)
    stage_counts = defaultdict(int)
    for row in rows:
        stage_counts[row["stage"]] += 1
    days_remaining = (
        (current_user.profile.exam_date - today).days
        if current_user.profile.exam_date
        else None
    )
    return render_template(
        "student/dashboard.html",
        plan=plan,
        today=today,
        today_tasks=today_tasks,
        today_completed=today_completed,
        today_planned=today_planned,
        snapshot=snapshot,
        workload=workload,
        week=week,
        week_start=week_start,
        upcoming=upcoming,
        deadlines=deadlines,
        subject_groups=dict(subject_groups),
        rows=rows,
        stage_counts=dict(stage_counts),
        days_remaining=days_remaining,
    )


@student_bp.post("/planner/generate")
@requires_onboarding
def generate_plan_route():
    active = StudyPlan.query.filter_by(
        user_id=current_user.id, status="active"
    ).first()
    try:
        plan = generate_study_plan(current_user, regenerate=bool(active))
    except PlanGenerationError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("student.dashboard"))
    db.session.refresh(plan)
    record_student_activity(current_user.id, "plan_generated", "study_plan", plan.id)
    flash(
        "Study plan regenerated. Completed tasks were preserved."
        if active
        else "Study plan generated. Review warnings before starting.",
        "success" if plan.feasible else "warning",
    )
    return redirect(url_for("student.dashboard"))


@student_bp.route("/planner/day")
@requires_onboarding
def daily_plan():
    refresh_overdue_tasks(current_user.id)
    selected = _parse_date(request.args.get("date")) or date.today()
    tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == current_user.id,
            StudyTask.scheduled_date == selected,
            StudyTask.status != "cancelled",
        )
        .order_by(StudyTask.start_time)
        .all()
    )
    commitments = commitments_for_day(current_user.id, selected)
    plan = (
        StudyPlan.query.filter_by(user_id=current_user.id, status="active")
        .order_by(StudyPlan.version_number.desc())
        .first()
    )
    completed = sum(
        (task.actual_minutes or task.estimated_minutes)
        for task in tasks
        if task.status == "completed"
    )
    planned = sum(task.estimated_minutes for task in tasks)
    slots = get_study_slots(current_user, selected)
    return render_template(
        "student/daily.html",
        selected=selected,
        tasks=tasks,
        commitments=commitments,
        plan=plan,
        completed=completed,
        planned=planned,
        free_minutes=sum(slot.minutes for slot in slots),
        previous_day=selected - timedelta(days=1),
        next_day=selected + timedelta(days=1),
        today=date.today(),
    )


@student_bp.route("/planner/week")
@requires_onboarding
def weekly_plan():
    refresh_overdue_tasks(current_user.id)
    start_raw = request.args.get("start")
    start = _parse_date(start_raw)
    if start is None:
        today = date.today()
        start = today - timedelta(days=today.weekday())
    start = start - timedelta(days=start.weekday())
    end = start + timedelta(days=6)
    tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == current_user.id,
            StudyTask.scheduled_date >= start,
            StudyTask.scheduled_date <= end,
            StudyTask.status != "cancelled",
        )
        .order_by(StudyTask.scheduled_date, StudyTask.start_time)
        .all()
    )
    by_day = defaultdict(list)
    for task in tasks:
        by_day[task.scheduled_date].append(task)
    commitments = {
        start + timedelta(days=offset): commitments_for_day(
            current_user.id, start + timedelta(days=offset)
        )
        for offset in range(7)
    }
    summary = task_stats(current_user.id, start, end)
    days = [start + timedelta(days=offset) for offset in range(7)]
    return render_template(
        "student/weekly.html",
        start=start,
        end=end,
        days=days,
        by_day=by_day,
        commitments=commitments,
        summary=summary,
        previous_week=start - timedelta(days=7),
        next_week=start + timedelta(days=7),
    )


@student_bp.route("/planner/annual")
@requires_onboarding
def annual_plan():
    plan = (
        StudyPlan.query.filter_by(user_id=current_user.id, status="active")
        .order_by(StudyPlan.version_number.desc())
        .first()
    )
    today = date.today()
    selected_month = request.args.get("month", type=int) or today.month
    selected_year = request.args.get("year", type=int) or today.year
    try:
        month_start = date(selected_year, selected_month, 1)
    except ValueError:
        month_start = date(today.year, today.month, 1)
    month_end = date(
        month_start.year,
        month_start.month,
        calendar_module.monthrange(month_start.year, month_start.month)[1],
    )
    months = []
    if plan:
        cursor = date(plan.horizon_start.year, plan.horizon_start.month, 1)
        last = date(plan.horizon_end.year, plan.horizon_end.month, 1)
        guard = 0
        while cursor <= last and guard < 36:
            month_last = date(
                cursor.year,
                cursor.month,
                calendar_module.monthrange(cursor.year, cursor.month)[1],
            )
            month_tasks = StudyTask.query.filter(
                StudyTask.user_id == current_user.id,
                StudyTask.plan_id == plan.id,
                StudyTask.status != "cancelled",
                StudyTask.scheduled_date >= cursor,
                StudyTask.scheduled_date <= month_last,
            ).all()
            total_minutes = sum(task.estimated_minutes for task in month_tasks)
            completed_tasks = sum(1 for task in month_tasks if task.status == "completed")
            months.append(
                {
                    "start": cursor,
                    "label": cursor.strftime("%B %Y"),
                    "tasks": len(month_tasks),
                    "completed": completed_tasks,
                    "minutes": total_minutes,
                    "selected": cursor.year == month_start.year
                    and cursor.month == month_start.month,
                }
            )
            cursor = (
                date(cursor.year + 1, 1, 1)
                if cursor.month == 12
                else date(cursor.year, cursor.month + 1, 1)
            )
            guard += 1
    month_tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == current_user.id,
            StudyTask.scheduled_date >= month_start,
            StudyTask.scheduled_date <= month_end,
            StudyTask.status != "cancelled",
        )
        .order_by(StudyTask.scheduled_date, StudyTask.start_time)
        .all()
    )
    tasks_by_date = defaultdict(list)
    for task in month_tasks:
        tasks_by_date[task.scheduled_date].append(task)
    weeks = calendar_module.Calendar(firstweekday=0).monthdatescalendar(
        month_start.year, month_start.month
    )
    return render_template(
        "student/annual.html",
        plan=plan,
        months=months,
        month_start=month_start,
        month_end=month_end,
        tasks_by_date=tasks_by_date,
        weeks=weeks,
        previous_month=month_start - timedelta(days=1),
        next_month=month_end + timedelta(days=1),
        today=today,
    )


@student_bp.route("/subjects")
@requires_onboarding
def subjects():
    rows = chapter_progress_rows(current_user)
    groups = defaultdict(list)
    for row in rows:
        groups[row["subject"].name].append(row)
    return render_template("student/subjects.html", groups=dict(groups), rows=rows)


@student_bp.route("/workload")
@requires_onboarding
def workload():
    data = workload_rows(current_user)
    rows = data["rows"]
    summary = data["summary"]
    subject_id = request.args.get("subject", type=int)
    status = (request.args.get("status") or "").strip().lower()
    stage = (request.args.get("stage") or "").strip().lower()
    difficulty = (request.args.get("difficulty") or "").strip().lower()
    query = (request.args.get("q") or "").strip()
    revision_only = request.args.get("mode") == "revision_only"
    filtered = rows
    if subject_id:
        filtered = [row for row in filtered if row["subject"].id == subject_id]
    if status == "not_started":
        filtered = [row for row in filtered if row["status"] == "Not started"]
    elif status == "in_progress":
        filtered = [
            row
            for row in filtered
            if row["status"] not in {"Not started", "Complete"}
        ]
    elif status == "complete":
        filtered = [row for row in filtered if row["status"] == "Complete"]
    elif status == "at_risk":
        filtered = [row for row in filtered if row["at_risk"]]
    if stage in {"learning", "practice", "revision"}:
        filtered = [
            row
            for row in filtered
            if row["remaining"][stage] > 0 or row["done"][stage] > 0
        ]
    if difficulty in {"easy", "medium", "hard"}:
        filtered = [
            row for row in filtered if row["chapter"].difficulty == difficulty
        ]
    if revision_only:
        filtered = [row for row in filtered if row["study_mode"] == "revision_only"]
    if query:
        needle = query.casefold()
        filtered = [
            row
            for row in filtered
            if needle in row["chapter"].name.casefold()
            or needle in row["subject"].name.casefold()
        ]
    filtered_remaining = sum(row["total_remaining"] for row in filtered)
    subject_choices = sorted(
        {row["subject"] for row in rows}, key=lambda item: item.name
    )
    return render_template(
        "student/workload.html",
        rows=filtered,
        all_rows=rows,
        summary=summary,
        filtered_remaining=filtered_remaining,
        subject_choices=subject_choices,
        filters={
            "subject": subject_id,
            "status": status,
            "stage": stage,
            "difficulty": difficulty,
            "q": query,
            "mode": "revision_only" if revision_only else "",
        },
    )


@student_bp.post("/workload/<int:chapter_id>")
@requires_onboarding
def update_chapter_workload(chapter_id):
    chapter = next(
        (item for item in selected_chapters(current_user) if item.id == chapter_id),
        None,
    )
    if chapter is None:
        abort(404)
    confidence_value = (request.form.get("confidence") or "").strip()
    confidence = None
    if confidence_value:
        try:
            confidence = max(1, min(5, int(confidence_value)))
        except ValueError:
            confidence = None
    mode = (request.form.get("study_mode") or "first_time").strip().lower()
    if mode not in {"first_time", "revision_only"}:
        mode = "first_time"
    override = StudentChapterPlan.query.filter_by(
        user_id=current_user.id, syllabus_chapter_id=chapter.id
    ).first()
    if confidence is None and mode == "first_time":
        if override is not None:
            db.session.delete(override)
            db.session.commit()
        flash("Chapter workload preference reset to the subject default.", "success")
    else:
        if override is None:
            override = StudentChapterPlan(
                user_id=current_user.id, syllabus_chapter_id=chapter.id
            )
            db.session.add(override)
        override.confidence = confidence
        override.study_mode = mode
        db.session.commit()
        record_student_activity(
            current_user.id, "chapter_workload_updated", "syllabus_chapter", chapter.id
        )
        flash(
            "Chapter workload preference saved. Regenerate the plan to apply it to future tasks.",
            "success",
        )
    return redirect(_safe_return(url_for("student.workload")))


@student_bp.route("/subjects/chapters/<int:chapter_id>")
@requires_onboarding
def chapter_detail(chapter_id):
    rows = chapter_progress_rows(current_user)
    row = next((item for item in rows if item["chapter"].id == chapter_id), None)
    if row is None:
        abort(404)
    tasks = (
        StudyTask.query.filter_by(
            user_id=current_user.id, syllabus_chapter_id=chapter_id
        )
        .filter(StudyTask.status != "cancelled")
        .order_by(StudyTask.scheduled_date, StudyTask.start_time)
        .all()
    )
    revisions = (
        RevisionRecord.query.filter_by(
            user_id=current_user.id, syllabus_chapter_id=chapter_id
        )
        .order_by(RevisionRecord.revision_number)
        .all()
    )
    workload = chapter_workload(current_user, row["chapter"])
    return render_template(
        "student/chapter.html",
        row=row,
        tasks=tasks,
        revisions=revisions,
        workload=workload,
    )


@student_bp.route("/revision")
@requires_onboarding
def revision_tracker():
    rows = [row for row in chapter_progress_rows(current_user) if row["tasks"] or row["revision_total"]]
    revision_tasks = (
        StudyTask.query.filter(
            StudyTask.user_id == current_user.id,
            StudyTask.task_type == "revision",
            StudyTask.status != "cancelled",
        )
        .order_by(StudyTask.scheduled_date, StudyTask.start_time)
        .all()
    )
    records = (
        RevisionRecord.query.filter_by(user_id=current_user.id)
        .order_by(RevisionRecord.scheduled_date)
        .all()
    )
    chapter_map = {row["chapter"].id: row for row in rows}
    return render_template(
        "student/revision.html",
        rows=rows,
        revision_tasks=revision_tasks,
        records=records,
        chapter_map=chapter_map,
    )


@student_bp.route("/mock-tests", methods=["GET", "POST"])
@requires_onboarding
def mock_tests():
    form = MockTestForm()
    form.set_subject_choices(current_user.subjects)
    if form.validate_on_submit():
        scheduled = form.scheduled_date.data
        if scheduled < date.today():
            flash("Choose a future date for the mock test.", "error")
        else:
            subject_id = form.subject_id.data or None
            if subject_id:
                allowed = {
                    link.syllabus_subject_id for link in current_user.subjects if link.is_active
                }
                if subject_id not in allowed:
                    flash("That subject is not assigned to your account.", "error")
                    return redirect(url_for("student.mock_tests"))
            db.session.add(
                MockTest(
                    user_id=current_user.id,
                    syllabus_subject_id=subject_id,
                    title=form.title.data,
                    scheduled_date=scheduled,
                    duration_minutes=form.duration_minutes.data,
                    notes=form.notes.data,
                    status="pending",
                )
            )
            db.session.commit()
            flash("Mock test added to your calendar.", "success")
            return redirect(url_for("student.mock_tests"))
    tests = (
        MockTest.query.filter(
            MockTest.user_id == current_user.id, MockTest.status != "cancelled"
        )
        .order_by(MockTest.scheduled_date.desc())
        .all()
    )
    pending = [test for test in tests if test.status == "pending"]
    completed = [test for test in tests if test.status == "completed"]
    scores = ScoreForm()
    return render_template(
        "student/mock_tests.html",
        form=form,
        tests=tests,
        pending=pending,
        completed=completed,
        scores=scores,
    )


@student_bp.post("/mock-tests/<int:mock_id>/result")
@requires_onboarding
def mock_test_result(mock_id):
    mock = MockTest.query.filter_by(
        id=mock_id, user_id=current_user.id
    ).first_or_404()
    form = ScoreForm()
    if not form.validate_on_submit():
        flash("Enter valid score details.", "error")
        return redirect(url_for("student.mock_tests"))
    score = form.score.data
    max_score = form.max_score.data
    if score is not None and max_score is not None and score > max_score:
        flash("Score cannot exceed the maximum score.", "error")
        return redirect(url_for("student.mock_tests"))
    mock.score = score
    mock.max_score = max_score
    if form.notes.data:
        mock.notes = form.notes.data
    mock.status = "completed"
    mock.completed_date = date.today()
    if mock.task_id:
        task = StudyTask.query.filter_by(
            id=mock.task_id, user_id=current_user.id
        ).first()
        if task and task.status != "completed":
            try:
                set_task_status(current_user, task, "completed")
            except TaskTransitionError:
                db.session.rollback()
    db.session.commit()
    flash("Mock-test result saved.", "success")
    return redirect(url_for("student.mock_tests"))


@student_bp.route("/recovery", methods=["GET", "POST"])
@requires_onboarding
def recovery():
    try:
        if request.method == "POST":
            result = apply_recovery(current_user)
            flash(
                f"{result['moved']} task(s) rescheduled across future days."
                if result["moved"]
                else "No tasks could be rescheduled.",
                "success" if result["moved"] else "warning",
            )
            for warning in result["warnings"]:
                flash(warning, "warning")
            return redirect(url_for("student.recovery"))
        preview = preview_recovery(current_user)
    except RecoveryError as exc:
        flash(str(exc), "error")
        preview = {
            "tasks": [],
            "unplaced": [],
            "total_minutes": 0,
            "available_minutes": 0,
            "feasible": True,
            "warnings": [],
            "proposals": [],
        }
    return render_template("student/recovery.html", preview=preview)


@student_bp.route("/settings/schedule", methods=["GET", "POST"])
@requires_onboarding
def schedule_settings():
    preferences = current_user.preferences
    form = SecureForm()
    if request.method == "POST" and form.validate_on_submit():
        try:
            preferred_start = parse_clock(request.form.get("preferred_study_start"))
            preferred_end = parse_clock(request.form.get("preferred_study_end"))
            wake = parse_clock(request.form.get("wake_time"))
            sleep = parse_clock(request.form.get("sleep_time"))
            if None in (preferred_start, preferred_end, wake, sleep):
                raise ValueError("Enter valid times for sleep, wake and study preferences.")
            session = _int_field("session_duration_minutes", 20, 120, "Session duration")
            break_minutes = _int_field("break_duration_minutes", 5, 60, "Break duration")
            break_frequency = _int_field(
                "break_frequency_sessions", 1, 5, "Break frequency"
            )
            daily_cap = _int_field("daily_cap_minutes", 30, 600, "Daily cap")
            revision_frequency = _int_field("revision_frequency", 1, 3, "Revision frequency")
            lighter_days = sorted(
                {
                    int(value)
                    for value in request.form.getlist("lighter_days")
                    if value.strip().isdigit() and 0 <= int(value) <= 6
                }
            )
            errors = validate_time_settings(
                wake=wake,
                sleep=sleep,
                preferred_start=preferred_start,
                preferred_end=preferred_end,
                session_minutes=session,
                break_minutes=break_minutes,
                daily_cap=daily_cap,
                revision_frequency=revision_frequency,
                lighter_days=lighter_days,
            )
            if errors:
                raise ValueError(" ".join(errors))
            preferences.wake_time = wake
            preferences.sleep_time = sleep
            preferences.preferred_study_start = preferred_start
            preferences.preferred_study_end = preferred_end
            preferences.session_duration_minutes = session
            preferences.break_duration_minutes = break_minutes
            preferences.break_frequency_sessions = break_frequency
            preferences.daily_cap_minutes = daily_cap
            preferences.revision_frequency = revision_frequency
            preferences.lighter_days = lighter_days
            preferences.struggle_notes = (
                request.form.get("struggle_notes") or ""
            ).strip()[:2000]
            db.session.commit()
            flash("Schedule settings saved. Regenerate your plan to apply changes.", "success")
            return redirect(url_for("student.schedule_settings"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    commitments = (
        WeeklyCommitment.query.filter_by(user_id=current_user.id, is_active=True)
        .order_by(WeeklyCommitment.day_of_week, WeeklyCommitment.start_time)
        .all()
    )
    by_day = defaultdict(list)
    for item in commitments:
        by_day[item.day_of_week].append(item)
    return render_template(
        "student/schedule_settings.html",
        form=form,
        preferences=preferences,
        commitments=commitments,
        by_day=by_day,
        days=DAY_NAMES,
    )


@student_bp.post("/settings/schedule/commitments")
@requires_onboarding
def add_commitment():
    day_raw = request.form.get("day_of_week", "0")
    try:
        day = int(day_raw)
    except ValueError:
        flash("Choose a valid weekday.", "error")
        return redirect(url_for("student.schedule_settings"))
    if day < 0 or day > 6:
        flash("Choose a valid weekday.", "error")
        return redirect(url_for("student.schedule_settings"))
    title = (request.form.get("title") or "").strip()[:120]
    kind = request.form.get("commitment_type", "other")
    if kind not in {"school", "tuition", "other"}:
        kind = "other"
    start = parse_clock(request.form.get("start_time"))
    end = parse_clock(request.form.get("end_time"))
    before = _travel_field("travel_before_minutes")
    after = _travel_field("travel_after_minutes")
    if not title or start is None or end is None or end <= start:
        flash("Enter a title, valid start and end, and a duration greater than zero.", "error")
        return redirect(url_for("student.schedule_settings"))
    existing = WeeklyCommitment.query.filter_by(
        user_id=current_user.id, is_active=True
    ).all()
    entries = [
        {
            "day": item.day_of_week,
            "title": item.title,
            "start": time_to_minutes(item.start_time) - item.travel_before_minutes,
            "end": time_to_minutes(item.end_time) + item.travel_after_minutes,
        }
        for item in existing
    ]
    entries.append(
        {
            "day": day,
            "title": title,
            "start": time_to_minutes(start) - before,
            "end": time_to_minutes(end) + after,
        }
    )
    errors = _validate_commitment_entries(entries)
    if errors:
        flash(" ".join(errors), "error")
        return redirect(url_for("student.schedule_settings"))
    db.session.add(
        WeeklyCommitment(
            user_id=current_user.id,
            day_of_week=day,
            commitment_type=kind,
            title=title,
            start_time=start,
            end_time=end,
            travel_before_minutes=before,
            travel_after_minutes=after,
            is_active=True,
        )
    )
    db.session.commit()
    flash("Fixed commitment added.", "success")
    return redirect(url_for("student.schedule_settings"))


@student_bp.post("/settings/schedule/commitments/<int:commitment_id>/delete")
@requires_onboarding
def delete_commitment(commitment_id):
    commitment = WeeklyCommitment.query.filter_by(
        id=commitment_id, user_id=current_user.id
    ).first_or_404()
    commitment.is_active = False
    db.session.commit()
    flash("Commitment removed from your weekly schedule.", "success")
    return redirect(url_for("student.schedule_settings"))


@student_bp.route("/profile", methods=["GET", "POST"])
@requires_onboarding
def profile_page():
    form = ProfileForm(obj=current_user)
    if form.validate_on_submit():
        current_user.full_name = form.full_name.data.strip()
        exam_raw = (request.form.get("exam_date") or "").strip()
        exam_date = _parse_date(exam_raw) if exam_raw else None
        if exam_raw and exam_date is None:
            flash("Enter a valid examination date.", "error")
        else:
            if exam_date and exam_date < date.today():
                flash("Examination date cannot be in the past.", "error")
            else:
                current_user.profile.exam_date = exam_date
                primary = ExamDate.query.filter_by(
                    user_id=current_user.id, is_primary=True
                ).first()
                if exam_date:
                    if primary is None:
                        primary = ExamDate(
                            user_id=current_user.id,
                            title="Board Examination",
                            exam_type="board",
                            is_primary=True,
                            exam_date=exam_date,
                        )
                        db.session.add(primary)
                    primary.exam_date = exam_date
                elif primary is not None:
                    db.session.delete(primary)
                db.session.commit()
                record_student_activity(
                    current_user.id, "profile_updated", "user", current_user.id
                )
                flash("Profile updated. Regenerate your plan if the exam date changed.", "success")
                return redirect(url_for("student.profile_page"))
    return render_template(
        "student/profile.html", form=form, profile=current_user.profile
    )


@student_bp.post("/tasks/<int:task_id>/status")
@requires_onboarding
def task_status(task_id):
    task = StudyTask.query.filter_by(
        id=task_id, user_id=current_user.id
    ).first_or_404()
    target = request.form.get("target", "")
    try:
        changed = set_task_status(current_user, task, target)
    except TaskTransitionError as exc:
        flash(str(exc), "error")
    else:
        if changed:
            flash(
                "Task completed." if target == "completed" else "Task updated.",
                "success",
            )
        else:
            flash("No change was needed. This task was already in that state.", "info")
    return redirect(_safe_return(url_for("student.daily_plan")))


@student_bp.post("/tasks/<int:task_id>/postpone")
@requires_onboarding
def postpone(task_id):
    task = StudyTask.query.filter_by(
        id=task_id, user_id=current_user.id
    ).first_or_404()
    mode = request.form.get("mode", "next")
    requested = _parse_date(request.form.get("new_date"))
    if mode == "date" and requested is None:
        flash("Choose a valid future date.", "error")
        return redirect(_safe_return(url_for("student.daily_plan")))
    try:
        changed, message = postpone_task(current_user, task, mode, requested)
    except TaskTransitionError as exc:
        flash(str(exc), "error")
    else:
        flash(message, "success" if changed else "warning")
    return redirect(_safe_return(url_for("student.daily_plan")))


@student_bp.post("/tasks/<int:task_id>/split")
@requires_onboarding
def split(task_id):
    task = StudyTask.query.filter_by(
        id=task_id, user_id=current_user.id
    ).first_or_404()
    try:
        parts = int(request.form.get("parts", "2"))
    except ValueError:
        parts = 2
    try:
        changed, message = split_task(current_user, task, parts)
        flash(message, "success" if changed else "warning")
    except TaskTransitionError as exc:
        flash(str(exc), "error")
    return redirect(_safe_return(url_for("student.daily_plan")))
