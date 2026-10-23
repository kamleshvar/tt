from flask import flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import auth_bp
from app.auth.forms import ChangePasswordForm, LoginForm, RegistrationForm
from app.models import User
from app.security import (
    is_safe_redirect_target,
    normalize_email,
    password_problem,
)
from app.services.accounts import (
    AccountError,
    create_student,
    record_student_activity,
    touch_login,
)
from extensions import db, limiter


def _post_login_destination(user: User) -> str:
    if user.must_change_password:
        return url_for("auth.change_password", required=1)
    if not user.onboarding_complete:
        return url_for("student.onboarding")
    return url_for("student.dashboard")


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("8 per minute", methods=["POST"])
def register():
    if current_user.is_authenticated and not current_user.must_change_password:
        return redirect(_post_login_destination(current_user))
    form = RegistrationForm()
    if form.validate_on_submit():
        problem = password_problem(form.password.data)
        if problem:
            flash(problem, "error")
            return render_template("auth/register.html", form=form), 400
        try:
            user = create_student(
                full_name=form.full_name.data,
                email=form.email.data,
                password=form.password.data,
                board=form.board.data,
                academic_year=form.academic_year.data,
            )
        except AccountError as exc:
            flash(str(exc), "error")
            return render_template("auth/register.html", form=form), 400
        login_user(user)
        session.permanent = True
        session["student_auth_version"] = user.auth_version
        touch_login(user)
        record_student_activity(user.id, "account_registered", "user", user.id)
        flash("Your Class 12 account is ready. Let’s build your study plan.", "success")
        return redirect(url_for("student.onboarding"))
    if form.errors:
        flash("Please check the highlighted fields.", "error")
    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("12 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated and not current_user.must_change_password:
        return redirect(_post_login_destination(current_user))
    form = LoginForm()
    if form.validate_on_submit():
        email = normalize_email(form.email.data)
        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(form.password.data):
            flash("Email or password is incorrect.", "error")
            return render_template("auth/login.html", form=form), 401
        if not user.is_active:
            flash("This account is deactivated. Please contact the administrator.", "error")
            return render_template("auth/login.html", form=form), 403
        login_user(user, remember=form.remember.data)
        session.permanent = True
        session["student_auth_version"] = user.auth_version
        touch_login(user)
        record_student_activity(user.id, "login", "user", user.id)
        flash(f"Welcome back, {user.full_name.split()[0]}.", "success")
        nxt = request.args.get("next")
        if nxt and is_safe_redirect_target(nxt, request.host_url):
            return redirect(nxt)
        return redirect(_post_login_destination(user))
    if form.errors:
        flash("Enter a valid email and password.", "error")
    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    user_id = current_user.id
    logout_user()
    session.clear()
    record_student_activity(user_id, "logout", "user", user_id)
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    forced = current_user.must_change_password
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Current password is incorrect.", "error")
            return render_template("auth/change_password.html", form=form, forced=forced), 401
        problem = password_problem(form.new_password.data)
        if problem:
            flash(problem, "error")
            return render_template("auth/change_password.html", form=form, forced=forced), 400
        if current_user.check_password(form.new_password.data):
            flash("Choose a password different from your current password.", "error")
            return render_template("auth/change_password.html", form=form, forced=forced), 400
        current_user.set_password(form.new_password.data)
        current_user.must_change_password = False
        current_user.auth_version = (current_user.auth_version or 0) + 1
        db.session.commit()
        session["student_auth_version"] = current_user.auth_version
        record_student_activity(
            current_user.id, "password_changed", "user", current_user.id
        )
        flash("Password updated.", "success")
        if current_user.onboarding_complete:
            return redirect(url_for("student.dashboard"))
        return redirect(url_for("student.onboarding"))
    if form.errors:
        flash("Please check the password fields.", "error")
    return render_template("auth/change_password.html", form=form, forced=forced)
