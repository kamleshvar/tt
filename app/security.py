import hmac
import re
from functools import wraps
from urllib.parse import urljoin, urlparse

from flask import abort, current_app, flash, redirect, request, session, url_for
from flask_login import current_user


def normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def password_problem(password: str | None) -> str | None:
    if not password or len(password) < 10:
        return "Use at least 10 characters."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Include at least one letter and one number."
    if password.lower() in {"password12", "passw0rd123", "administrator1"}:
        return "Choose a less predictable password."
    return None


def is_safe_redirect_target(target: str | None, host_url: str) -> bool:
    if not target:
        return False
    ref_url = urlparse(host_url)
    test_url = urlparse(urljoin(host_url, target))
    return test_url.scheme in {"http", "https"} and ref_url.netloc == test_url.netloc


def admin_password_matches(candidate: str) -> bool:
    configured = current_app.config.get("ADMIN_PASSWORD")
    if not configured or not candidate:
        return False
    return hmac.compare_digest(str(configured).encode("utf-8"), candidate.encode("utf-8"))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        from datetime import datetime, timezone

        if not session.get("admin_authenticated"):
            return redirect(url_for("admin.login", next=request.path))
        authenticated_at = session.get("admin_authenticated_at", 0)
        timeout = current_app.config.get("ADMIN_SESSION_MINUTES", 20) * 60
        if not authenticated_at or (
            datetime.now(timezone.utc).timestamp() - float(authenticated_at) > timeout
        ):
            session.pop("admin_authenticated", None)
            session.pop("admin_authenticated_at", None)
            flash("Administrator session expired. Please sign in again.", "warning")
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)

    return wrapped


def student_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("auth.login", next=request.path))
        if (
            current_user.auth_version
            != session.get("student_auth_version", current_user.auth_version)
        ):
            abort(401)
        return view(*args, **kwargs)

    return wrapped


def audit_metadata() -> str:
    ua = (request.user_agent.string or "")[:255]
    return ua


def redact_secrets(text: str) -> str:
    patterns = [
        (r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)\s*[=:]\s*\S+", r"\1=[REDACTED]"),
        (r"(?i)(postgres(?:ql)?(?:\+psycopg2)?://[^:\s]+:)[^@\s]+@", r"\1[REDACTED]@"),
    ]
    redacted = text
    for pattern, replacement in patterns:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted
