from flask import request
from sqlalchemy.exc import IntegrityError

from app.models import (
    StudentActivityLog,
    StudentPreferences,
    StudentProfile,
    SystemSetting,
    User,
    utcnow,
)
from app.security import normalize_email
from extensions import db


class AccountError(Exception):
    pass


def create_student(
    *,
    full_name: str,
    email: str,
    password: str,
    board: str,
    academic_year: str,
) -> User:
    normalized_email = normalize_email(email)
    if not normalized_email:
        raise AccountError("An email address is required.")
    if User.query.filter_by(email=normalized_email).first():
        raise AccountError("An account with that email already exists.")
    user = User(full_name=full_name.strip(), email=normalized_email)
    user.set_password(password)
    user.profile = StudentProfile(
        class_level="Class 12", board=board, academic_year=academic_year
    )

    def _setting_int(key, default, minimum, maximum):
        try:
            value = int(SystemSetting.get_value(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    user.preferences = StudentPreferences(
        session_duration_minutes=_setting_int(
            "default_session_duration_minutes", 45, 20, 120
        ),
        daily_cap_minutes=_setting_int("default_daily_cap_minutes", 240, 30, 600),
        revision_frequency=_setting_int("default_revision_frequency", 2, 1, 3),
    )
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise AccountError("An account with that email already exists.")
    return user


def record_student_activity(
    user_id: int | None, action: str, entity_type: str | None = None, entity_id: int | None = None
) -> None:
    try:
        db.session.add(
            StudentActivityLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                ip_address=(request.remote_addr or "")[:45] if request else None,
                user_agent=(request.user_agent.string or "")[:255] if request else None,
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def reset_student_password(user: User, new_password: str) -> None:
    user.set_password(new_password)
    user.must_change_password = True
    user.auth_version = (user.auth_version or 0) + 1
    db.session.commit()


def set_student_active(user: User, active: bool) -> None:
    user.is_active = active
    if not active:
        user.auth_version = (user.auth_version or 0) + 1
    db.session.commit()


def touch_login(user: User) -> None:
    user.last_login_at = utcnow()
    db.session.commit()
