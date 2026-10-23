from __future__ import annotations

import json
import re
from datetime import datetime, date, timezone
from urllib.parse import urlparse

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, text
from werkzeug.utils import secure_filename

from app.admin import admin_bp
from app.admin.forms import AdminLoginForm
from app.auth.forms import SecureForm
from app.models import (
    AdminAuditLog,
    ExamDate,
    StudentSubject,
    StudyPlan,
    StudyTask,
    StudentActivityLog,
    SyllabusChapter,
    SyllabusSubject,
    SyllabusVersion,
    SystemSetting,
    User,
    utcnow,
)
from app.planner.engine import PlanGenerationError, generate_study_plan
from app.planner.progress import progress_snapshot
from app.security import (
    admin_password_matches,
    admin_required,
    password_problem,
    redact_secrets,
)
from app.services.accounts import reset_student_password, set_student_active
from app.syllabus.loader import (
    SyllabusImportError,
    import_syllabus,
    read_syllabus_payload,
    validate_syllabus_payload,
)
from app.syllabus.validator import validate_version_structure
from extensions import db, limiter


def _audit(action: str, entity_type: str | None = None, entity_id: int | None = None, details: str | None = None):
    db.session.add(
        AdminAuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            ip_address=(request.remote_addr or "")[:45],
        )
    )
    db.session.commit()


def _is_admin_authenticated() -> bool:
    if not session.get("admin_authenticated"):
        return False
    authenticated_at = float(session.get("admin_authenticated_at", 0))
    timeout = current_app.config.get("ADMIN_SESSION_MINUTES", 20) * 60
    return bool(authenticated_at) and (
        datetime.now(timezone.utc).timestamp() - authenticated_at <= timeout
    )


@admin_bp.route("/", methods=["GET", "POST"], strict_slashes=False)
@limiter.limit("6 per minute", methods=["POST"])
def login():
    if _is_admin_authenticated():
        return redirect(url_for("admin.dashboard"))
    if not current_app.config.get("ADMIN_PASSWORD"):
        flash(
            "Administrator login is not configured. Set ADMIN_PASSWORD in the "
            "server environment and restart.",
            "error",
        )
        return render_template("admin/login.html", form=AdminLoginForm()), 503
    form = AdminLoginForm()
    if form.validate_on_submit():
        if not admin_password_matches(form.password.data):
            _audit("admin_login_failed", details="Incorrect administrator password.")
            flash("Incorrect administrator password.", "error")
            return render_template("admin/login.html", form=form), 401
        session.clear()
        session["admin_authenticated"] = True
        session["admin_authenticated_at"] = datetime.now(timezone.utc).timestamp()
        session.permanent = False
        _audit("admin_login", details="Password-only administrator login.")
        flash("Administrator session started.", "success")
        return redirect(url_for("admin.dashboard"))
    if form.errors:
        flash("Enter the administrator password.", "error")
    return render_template("admin/login.html", form=form)


@admin_bp.post("/logout")
@admin_required
def logout():
    _audit("admin_logout", details="Administrator session ended.")
    session.clear()
    flash("Administrator signed out.", "success")
    return redirect(url_for("admin.login"))


@admin_bp.route("/dashboard")
@admin_required
def dashboard():
    total_students = User.query.count()
    active_students = User.query.filter_by(is_active=True).count()
    inactive_students = total_students - active_students
    onboarded = User.query.join(User.profile).filter_by(onboarding_completed=True).count()
    active_plans = StudyPlan.query.filter_by(status="active").all()
    plan_issues = [plan for plan in active_plans if not plan.feasible]
    users_with_plans = (
        db.session.query(func.count(func.distinct(StudyPlan.user_id)))
        .filter(StudyPlan.status == "active")
        .scalar()
        or 0
    )
    versions = SyllabusVersion.query.order_by(
        SyllabusVersion.board, SyllabusVersion.academic_year, SyllabusVersion.imported_at.desc()
    ).all()
    current_versions = [version for version in versions if version.is_current]
    db_ok = True
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        db.session.rollback()
        db_ok = False
    recent_audit = (
        AdminAuditLog.query.order_by(AdminAuditLog.created_at.desc()).limit(12).all()
    )
    return render_template(
        "admin/dashboard.html",
        total_students=total_students,
        active_students=active_students,
        inactive_students=inactive_students,
        onboarded=onboarded,
        active_plans=len(active_plans),
        plan_issues=len(plan_issues),
        users_with_plans=users_with_plans,
        versions=versions,
        current_versions=current_versions,
        db_ok=db_ok,
        recent_audit=recent_audit,
        app_version=current_app.config.get("APP_VERSION", "1.0.0"),
    )


@admin_bp.route("/students")
@admin_required
def students():
    query = User.query
    search = (request.args.get("q") or "").strip()[:120]
    status = request.args.get("status", "all")
    onboarding = request.args.get("onboarding", "all")
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(User.email.ilike(like), User.full_name.ilike(like))
        )
    if status == "active":
        query = query.filter(User.is_active.is_(True))
    elif status == "inactive":
        query = query.filter(User.is_active.is_(False))
    if onboarding == "complete":
        query = query.join(User.profile).filter_by(onboarding_completed=True)
    elif onboarding == "incomplete":
        query = query.join(User.profile).filter_by(onboarding_completed=False)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=max(1, page), per_page=20, error_out=False
    )
    plan_counts = dict(
        db.session.query(StudyPlan.user_id, func.count(StudyPlan.id))
        .filter(StudyPlan.user_id.in_([user.id for user in pagination.items]))
        .group_by(StudyPlan.user_id)
        .all()
    ) if pagination.items else {}
    return render_template(
        "admin/students.html",
        pagination=pagination,
        students=pagination.items,
        search=search,
        status=status,
        onboarding=onboarding,
        plan_counts=plan_counts,
    )


@admin_bp.route("/students/<int:user_id>")
@admin_required
def student_detail(user_id):
    student = db.session.get(User, user_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin.students"))
    plan = (
        StudyPlan.query.filter_by(user_id=student.id, status="active")
        .order_by(StudyPlan.version_number.desc())
        .first()
    )
    snapshot = progress_snapshot(student) if student.onboarding_complete else None
    tasks = (
        StudyTask.query.filter_by(user_id=student.id)
        .filter(StudyTask.status != "cancelled")
        .order_by(StudyTask.scheduled_date.desc())
        .limit(30)
        .all()
    )
    activity = (
        StudentActivityLog.query.filter_by(user_id=student.id)
        .order_by(StudentActivityLog.created_at.desc())
        .limit(20)
        .all()
    )
    audits = (
        AdminAuditLog.query.filter_by(entity_type="user", entity_id=student.id)
        .order_by(AdminAuditLog.created_at.desc())
        .limit(20)
        .all()
    )
    subjects = [
        link for link in student.subjects if link.is_active and link.syllabus_subject
    ]
    exams = (
        ExamDate.query.filter_by(user_id=student.id)
        .order_by(ExamDate.exam_date)
        .all()
    )
    return render_template(
        "admin/student_detail.html",
        student=student,
        plan=plan,
        snapshot=snapshot,
        tasks=tasks,
        activity=activity,
        audits=audits,
        subjects=subjects,
        exams=exams,
    )


@admin_bp.post("/students/<int:user_id>/toggle")
@admin_required
def toggle_student(user_id):
    student = db.session.get(User, user_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin.students"))
    new_state = not student.is_active
    set_student_active(student, new_state)
    _audit(
        "student_activated" if new_state else "student_deactivated",
        "user",
        student.id,
        details=f"Account {'restored' if new_state else 'deactivated'} by administrator.",
    )
    flash(
        f"{student.full_name} has been {'restored' if new_state else 'deactivated'}.",
        "success",
    )
    return redirect(url_for("admin.student_detail", user_id=student.id))


@admin_bp.post("/students/<int:user_id>/reset-password")
@admin_required
def reset_password(user_id):
    student = db.session.get(User, user_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin.students"))
    new_password = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""
    problem = password_problem(new_password)
    if problem:
        flash(problem, "error")
        return redirect(url_for("admin.student_detail", user_id=student.id))
    if new_password != confirm:
        flash("Temporary passwords do not match.", "error")
        return redirect(url_for("admin.student_detail", user_id=student.id))
    reset_student_password(student, new_password)
    _audit(
        "password_reset",
        "user",
        student.id,
        details="Administrator reset the password; change is required at next login.",
    )
    flash(
        "Temporary password set. The student must change it after signing in. "
        "Give it to the student through a secure channel.",
        "success",
    )
    return redirect(url_for("admin.student_detail", user_id=student.id))


@admin_bp.post("/students/<int:user_id>/regenerate")
@admin_required
def regenerate_student_plan(user_id):
    student = db.session.get(User, user_id)
    if student is None:
        flash("Student not found.", "error")
        return redirect(url_for("admin.students"))
    if request.form.get("confirm") != "yes":
        flash("Explicit confirmation is required to regenerate a student plan.", "error")
        return redirect(url_for("admin.student_detail", user_id=student.id))
    try:
        plan = generate_study_plan(student, regenerate=True)
    except PlanGenerationError as exc:
        db.session.rollback()
        _audit(
            "plan_regeneration_failed",
            "user",
            student.id,
            details=str(exc)[:500],
        )
        flash(f"Plan was not regenerated: {exc}", "error")
        return redirect(url_for("admin.student_detail", user_id=student.id))
    _audit(
        "plan_regenerated",
        "user",
        student.id,
        details=f"Plan version {plan.version_number}; completed tasks preserved.",
    )
    flash(
        f"Plan regenerated for {student.full_name}. Completed tasks were preserved.",
        "success" if plan.feasible else "warning",
    )
    return redirect(url_for("admin.student_detail", user_id=student.id))


@admin_bp.route("/syllabus", methods=["GET", "POST"])
@admin_required
def syllabus():
    form = SecureForm()
    report = None
    versions = SyllabusVersion.query.order_by(
        SyllabusVersion.board,
        SyllabusVersion.academic_year,
        SyllabusVersion.imported_at.desc(),
    ).all()
    version_rows = []
    for version in versions:
        usage = (
            db.session.query(func.count(StudentSubject.id))
            .join(SyllabusSubject, StudentSubject.syllabus_subject_id == SyllabusSubject.id)
            .filter(SyllabusSubject.version_id == version.id)
            .scalar()
            or 0
        )
        version_rows.append(
            {
                "version": version,
                "usage": usage,
                "validation": validate_version_structure(version),
            }
        )
    if request.method == "POST" and form.validate_on_submit():
        payload = None
        source_name = None
        upload = request.files.get("syllabus_file")
        raw_text = (request.form.get("syllabus_json") or "").strip()
        try:
            if upload and upload.filename:
                filename = secure_filename(upload.filename)
                if not filename.lower().endswith(".json"):
                    raise SyllabusImportError("Upload a .json syllabus file.")
                raw = upload.read()
                if len(raw) > 2 * 1024 * 1024:
                    raise SyllabusImportError("Syllabus JSON must be smaller than 2 MB.")
                payload = json.loads(raw.decode("utf-8-sig"))
                source_name = f"admin-upload:{filename}"
            elif raw_text:
                if len(raw_text.encode("utf-8")) > 2 * 1024 * 1024:
                    raise SyllabusImportError("Syllabus JSON must be smaller than 2 MB.")
                payload = json.loads(raw_text)
                source_name = "admin-pasted-json"
            else:
                raise SyllabusImportError("Upload a syllabus JSON file or paste JSON content.")
            report = validate_syllabus_payload(payload)
            if not report["valid"]:
                raise SyllabusImportError(
                    "Syllabus validation failed. Nothing was imported.", report["errors"]
                )
            make_current = request.form.get("make_current") == "yes"
            version = import_syllabus(
                payload, source_file=source_name, make_current=make_current
            )
            _audit(
                "syllabus_imported",
                "syllabus_version",
                version.id,
                details=(
                    f"{version.board} {version.academic_year} {version.version_label}; "
                    f"status={version.status}; current={version.is_current}"
                ),
            )
            flash(
                f"Syllabus {version.version_label} imported successfully with "
                f"{report['chapter_count']} chapters.",
                "success",
            )
            return redirect(url_for("admin.syllabus"))
        except json.JSONDecodeError as exc:
            flash(f"Invalid JSON: {exc}", "error")
        except SyllabusImportError as exc:
            report = report or {"errors": exc.errors or [str(exc)], "warnings": []}
            flash(str(exc), "error")
            for error in exc.errors[:8]:
                flash(error, "error")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Syllabus import failed")
            flash("The syllabus import failed unexpectedly. No partial data was saved.", "error")
    return render_template(
        "admin/syllabus.html",
        form=form,
        version_rows=version_rows,
        report=report,
    )


@admin_bp.post("/syllabus/<int:version_id>/activate")
@admin_required
def activate_syllabus(version_id):
    version = db.session.get(SyllabusVersion, version_id)
    if version is None:
        flash("Syllabus version not found.", "error")
        return redirect(url_for("admin.syllabus"))
    SyllabusVersion.query.filter(
        SyllabusVersion.board == version.board,
        SyllabusVersion.academic_year == version.academic_year,
        SyllabusVersion.id != version.id,
    ).update({"is_current": False}, synchronize_session=False)
    version.is_current = True
    db.session.commit()
    _audit(
        "syllabus_activated",
        "syllabus_version",
        version.id,
        details=f"{version.board} {version.academic_year} {version.version_label}",
    )
    flash(
        "Syllabus marked current for new onboarding. Existing student plans keep "
        "the version they were generated with.",
        "success",
    )
    return redirect(url_for("admin.syllabus"))


def _active_chapters(version):
    if version is None:
        return []
    return [
        chapter
        for subject in version.subjects
        if not subject.is_removed
        for unit in subject.units
        for chapter in unit.chapters
        if not chapter.is_removed
    ]


def _version_workload_summary(version):
    summary = {
        "chapters": 0,
        "learning_minutes": 0,
        "practice_minutes": 0,
        "revision_minutes": 0,
        "total_minutes": 0,
        "subjects": {},
    }
    if version is None:
        return summary
    for subject in version.subjects:
        if subject.is_removed:
            continue
        subject_total = 0
        count = 0
        for unit in subject.units:
            for chapter in unit.chapters:
                if chapter.is_removed:
                    continue
                count += 1
                subject_total += chapter.total_minutes
                summary["learning_minutes"] += chapter.learning_minutes or 0
                summary["practice_minutes"] += chapter.practice_minutes or 0
                summary["revision_minutes"] += chapter.revision_minutes or 0
        if count:
            summary["subjects"][subject.code] = {
                "name": subject.name,
                "chapters": count,
                "minutes": subject_total,
            }
            summary["chapters"] += count
    summary["total_minutes"] = (
        summary["learning_minutes"]
        + summary["practice_minutes"]
        + summary["revision_minutes"]
    )
    return summary


@admin_bp.route("/workload")
@admin_required
def workload_admin():
    versions = SyllabusVersion.query.order_by(
        SyllabusVersion.board,
        SyllabusVersion.academic_year,
        SyllabusVersion.imported_at.desc(),
    ).all()
    version_id = request.args.get("version", type=int)
    version = None
    if version_id:
        version = db.session.get(SyllabusVersion, version_id)
    if version is None:
        version = next((item for item in versions if item.is_current), None)
    if version is None and versions:
        version = versions[0]
    compare_id = request.args.get("compare", type=int)
    compare_version = db.session.get(SyllabusVersion, compare_id) if compare_id else None
    if compare_version is None and version is not None:
        compare_version = next(
            (item for item in versions if item.id != version.id), None
        )

    chapters = _active_chapters(version)
    subject_id = request.args.get("subject", type=int)
    confidence = (request.args.get("confidence") or "").strip().lower()
    difficulty = (request.args.get("difficulty") or "").strip().lower()
    research = (request.args.get("research") or "").strip().lower()
    query = (request.args.get("q") or "").strip()
    if subject_id:
        chapters = [
            chapter for chapter in chapters if chapter.subject.id == subject_id
        ]
    if confidence in {"low", "medium", "high"}:
        chapters = [
            chapter
            for chapter in chapters
            if (chapter.estimate_confidence or "").lower() == confidence
        ]
    if difficulty in {"easy", "medium", "hard"}:
        chapters = [
            chapter for chapter in chapters if (chapter.difficulty or "") == difficulty
        ]
    if research:
        chapters = [
            chapter
            for chapter in chapters
            if (chapter.research_status or "").lower() == research
        ]
    if query:
        needle = query.casefold()
        chapters = [
            chapter
            for chapter in chapters
            if needle in chapter.name.casefold()
            or needle in chapter.subject.name.casefold()
        ]
    chapters.sort(key=lambda chapter: (chapter.subject.order_index, chapter.unit.order_index, chapter.order_index))
    current_summary = _version_workload_summary(version)
    compare_summary = _version_workload_summary(compare_version)
    comparison = []
    codes = set(current_summary["subjects"]) | set(compare_summary["subjects"])
    for code in sorted(codes):
        current = current_summary["subjects"].get(code)
        other = compare_summary["subjects"].get(code)
        current_minutes = current["minutes"] if current else 0
        other_minutes = other["minutes"] if other else 0
        comparison.append(
            {
                "code": code,
                "name": (current or other)["name"],
                "current_minutes": current_minutes,
                "compare_minutes": other_minutes,
                "delta_minutes": current_minutes - other_minutes,
            }
        )
    low_confidence = sum(
        1
        for chapter in _active_chapters(version)
        if (chapter.estimate_confidence or "").lower() == "low"
    )
    filtered_totals = {
        "learning": sum(chapter.learning_minutes or 0 for chapter in chapters),
        "practice": sum(chapter.practice_minutes or 0 for chapter in chapters),
        "revision": sum(chapter.revision_minutes or 0 for chapter in chapters),
    }
    filtered_totals["total"] = sum(filtered_totals.values())
    subject_choices = sorted(
        {
            chapter.subject
            for chapter in _active_chapters(version)
        },
        key=lambda subject: subject.name,
    )
    return render_template(
        "admin/workload.html",
        versions=versions,
        version=version,
        compare_version=compare_version,
        chapters=chapters,
        current_summary=current_summary,
        compare_summary=compare_summary,
        comparison=comparison,
        filtered_totals=filtered_totals,
        low_confidence=low_confidence,
        subject_choices=subject_choices,
        filters={
            "subject": subject_id,
            "confidence": confidence,
            "difficulty": difficulty,
            "research": research,
            "q": query,
        },
    )


@admin_bp.post("/workload/chapter/<int:chapter_id>")
@admin_required
def update_chapter_estimate(chapter_id):
    chapter = db.session.get(SyllabusChapter, chapter_id)
    if chapter is None:
        flash("Chapter not found.", "error")
        return redirect(url_for("admin.workload_admin"))
    reason = (request.form.get("review_note") or "").strip()
    if len(reason) < 5:
        flash("Add a short review reason before saving an estimate change.", "error")
        return redirect(
            url_for(
                "admin.workload_admin",
                version=chapter.unit.subject.version_id,
            )
        )
    try:
        learning = int(round(float(request.form.get("learning_hours") or 0) * 60))
        practice = int(round(float(request.form.get("practice_hours") or 0) * 60))
        revision = int(round(float(request.form.get("revision_hours") or 0) * 60))
    except (TypeError, ValueError, OverflowError):
        flash("Enter numeric stage hours.", "error")
        return redirect(
            url_for(
                "admin.workload_admin",
                version=chapter.unit.subject.version_id,
            )
        )
    if min(learning, practice, revision) < 0:
        flash("Stage hours cannot be negative.", "error")
        return redirect(
            url_for(
                "admin.workload_admin",
                version=chapter.unit.subject.version_id,
            )
        )
    total = learning + practice + revision
    if total <= 0:
        flash("A chapter must have a positive total workload.", "error")
        return redirect(
            url_for(
                "admin.workload_admin",
                version=chapter.unit.subject.version_id,
            )
        )
    if total > 300 * 60:
        flash("That total is implausibly high for one chapter.", "error")
        return redirect(
            url_for(
                "admin.workload_admin",
                version=chapter.unit.subject.version_id,
            )
        )
    confidence = (request.form.get("estimate_confidence") or "medium").lower()
    difficulty = (request.form.get("difficulty") or "medium").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"
    previous = (
        f"L{chapter.learning_minutes or 0} P{chapter.practice_minutes or 0} "
        f"R{chapter.revision_minutes or 0} / {chapter.estimate_confidence}"
    )
    chapter.learning_minutes = learning
    chapter.practice_minutes = practice
    chapter.revision_minutes = revision
    chapter.estimated_minutes = total
    chapter.estimate_confidence = confidence
    chapter.difficulty = difficulty
    chapter.estimate_method = "administrator_review"
    chapter.estimate_review_note = reason
    chapter.estimate_updated_at = utcnow()
    chapter.research_status = "admin_reviewed"
    db.session.commit()
    _audit(
        "syllabus_estimate_updated",
        "syllabus_chapter",
        chapter.id,
        details=(
            f"{chapter.full_label}: {previous} -> "
            f"L{learning} P{practice} R{revision} / {confidence}. Reason: {reason[:300]}"
        ),
    )
    flash(f"Workload estimate updated for {chapter.name}.", "success")
    return redirect(
        url_for(
            "admin.workload_admin",
            version=chapter.unit.subject.version_id,
        )
    )


@admin_bp.route("/planner", methods=["GET", "POST"])
@admin_required
def planner_admin():
    if request.method == "POST":
        try:
            session_minutes = int(request.form.get("default_session_duration_minutes", "45"))
            daily_cap = int(request.form.get("default_daily_cap_minutes", "240"))
            revision_frequency = int(request.form.get("default_revision_frequency", "2"))
            if not 20 <= session_minutes <= 120:
                raise ValueError("Default session duration must be 20–120 minutes.")
            if not 30 <= daily_cap <= 600:
                raise ValueError("Default daily cap must be 30–600 minutes.")
            if not 1 <= revision_frequency <= 3:
                raise ValueError("Default revision frequency must be 1–3.")
            SystemSetting.set_value(
                "default_session_duration_minutes",
                session_minutes,
                "Applied to new student preference profiles.",
            )
            SystemSetting.set_value(
                "default_daily_cap_minutes",
                daily_cap,
                "Applied to new student preference profiles.",
            )
            SystemSetting.set_value(
                "default_revision_frequency",
                revision_frequency,
                "Applied to new student preference profiles.",
            )
            db.session.commit()
            _audit(
                "planner_defaults_updated",
                details=(
                    f"session={session_minutes}, cap={daily_cap}, "
                    f"revision={revision_frequency}"
                ),
            )
            flash("Planner defaults saved for new student accounts.", "success")
            return redirect(url_for("admin.planner_admin"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    active_plans = (
        StudyPlan.query.filter_by(status="active")
        .order_by(StudyPlan.generated_at.desc())
        .limit(100)
        .all()
    )
    problem_plans = [plan for plan in active_plans if not plan.feasible or plan.warnings]
    defaults = {
        "session": SystemSetting.get_value("default_session_duration_minutes", "45"),
        "cap": SystemSetting.get_value("default_daily_cap_minutes", "240"),
        "revision": SystemSetting.get_value("default_revision_frequency", "2"),
    }
    return render_template(
        "admin/planner.html",
        active_plans=active_plans,
        problem_plans=problem_plans,
        defaults=defaults,
    )


def _masked_database_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        database = (parsed.path or "").lstrip("/") or "(unknown)"
        host = parsed.hostname or "local"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}/{database}"
    except Exception:
        return "configured (details unavailable)"


def _tail_logs(max_lines: int = 120) -> list[str]:
    log_dir = current_app.config.get("LOG_DIR")
    candidates = []
    if log_dir:
        candidates.append(log_dir / "tt-generator.log")
    for candidate in candidates:
        try:
            if candidate.exists():
                with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()[-max_lines:]
                return [redact_secrets(line.rstrip()) for line in lines]
        except OSError:
            continue
    return []


@admin_bp.route("/system")
@admin_required
def system():
    db_status = "connected"
    alembic = "not available"
    try:
        db.session.execute(text("SELECT 1"))
        try:
            result = db.session.execute(text("SELECT version_num FROM alembic_version")).first()
            if result:
                alembic = result[0]
        except Exception:
            db.session.rollback()
            alembic = "not migrated (run flask db upgrade)"
    except Exception:
        db.session.rollback()
        db_status = "unavailable"
    env_status = {
        "SECRET_KEY": bool(current_app.config.get("SECRET_KEY")),
        "ADMIN_PASSWORD": bool(current_app.config.get("ADMIN_PASSWORD")),
        "SESSION_COOKIE_SECURE": bool(current_app.config.get("SESSION_COOKIE_SECURE")),
        "CSRF_PROTECTION": bool(current_app.config.get("WTF_CSRF_ENABLED")),
        "RATE_LIMITING": bool(current_app.config.get("RATELIMIT_ENABLED")),
        "SQLALCHEMY_DATABASE_URI": _masked_database_url(
            str(current_app.config.get("SQLALCHEMY_DATABASE_URI"))
        ),
    }
    return render_template(
        "admin/system.html",
        db_status=db_status,
        alembic=alembic,
        env_status=env_status,
        logs=_tail_logs(),
        app_version=current_app.config.get("APP_VERSION", "1.0.0"),
        environment=current_app.config.get("ENV_NAME", current_app.config.get("FLASK_ENV", "production")),
    )
