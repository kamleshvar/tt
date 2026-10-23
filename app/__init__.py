from __future__ import annotations

import logging
import os
import secrets
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import click
from flask import Flask, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, logout_user
from flask_wtf.csrf import CSRFError
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from config import BASE_DIR, get_config
from extensions import csrf, db, limiter, login_manager, migrate


def _configure_logging(app: Flask) -> None:
    if app.config.get("TESTING"):
        return
    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), logging.INFO)
    app.logger.setLevel(level)
    if any(isinstance(handler, RotatingFileHandler) for handler in app.logger.handlers):
        return
    if app.config.get("LOG_TO_FILE", True):
        log_dir = Path(app.config["LOG_DIR"])
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "tt-generator.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        app.logger.addHandler(handler)


def _validate_production_config(app: Flask, config_name: str) -> None:
    if config_name != "production":
        return
    secret = app.config.get("SECRET_KEY")
    admin = app.config.get("ADMIN_PASSWORD")
    if not secret or len(str(secret)) < 32 or "replace-with" in str(secret):
        raise RuntimeError(
            "Production refused to start: SECRET_KEY must be a long random value."
        )
    if not admin or len(str(admin)) < 12 or "replace-with" in str(admin):
        raise RuntimeError(
            "Production refused to start: ADMIN_PASSWORD must be configured with a strong value."
        )
    if str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("sqlite"):
        app.logger.warning(
            "Production is using SQLite. PostgreSQL is strongly recommended for deployment."
        )


def create_app(config_name: str | None = None, test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    resolved_name = (config_name or os.getenv("FLASK_ENV") or "development").strip().lower()
    app.config.from_object(get_config(resolved_name))
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("ENV_NAME", resolved_name)
    app.config["FLASK_ENV"] = resolved_name
    if not app.config.get("SECRET_KEY"):
        if resolved_name == "production":
            raise RuntimeError("Production refused to start: SECRET_KEY is missing.")
        app.config["SECRET_KEY"] = secrets.token_hex(32)
    _validate_production_config(app, resolved_name)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    _configure_logging(app)

    if not app.config.get("ASSET_VERSION"):
        asset_paths = [
            Path(app.static_folder) / "css" / "app.css",
            Path(app.static_folder) / "js" / "app.js",
        ]
        try:
            app.config["ASSET_VERSION"] = str(
                max(int(path.stat().st_mtime) for path in asset_paths)
            )
        except OSError:
            app.config["ASSET_VERSION"] = app.config["APP_VERSION"]

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"
    csrf.init_app(app)
    limiter.init_app(app)

    if app.config.get("TRUST_PROXY", resolved_name == "production"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            user = db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None
        if user is None or not user.is_active:
            return None
        return user

    from app.admin import admin_bp
    from app.auth import auth_bp
    from app.student import student_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(admin_bp)

    @app.before_request
    def enforce_account_state():
        if not current_user.is_authenticated:
            return None
        if not current_user.is_active:
            logout_user()
            session.clear()
            flash("This account is no longer active.", "error")
            return redirect(url_for("auth.login"))
        if current_user.profile is None or current_user.profile.class_level != "Class 12":
            logout_user()
            session.clear()
            flash("TT Generator student accounts are restricted to Class 12.", "error")
            return redirect(url_for("auth.login"))
        stored_version = session.get("student_auth_version")
        if stored_version is not None and stored_version != current_user.auth_version:
            logout_user()
            session.clear()
            flash("Your session was invalidated. Sign in again with your new credentials.", "info")
            return redirect(url_for("auth.login"))
        if current_user.must_change_password:
            allowed = {"auth.change_password", "auth.logout", "static"}
            if request.endpoint not in allowed:
                flash("Set a new password before continuing.", "info")
                return redirect(url_for("auth.change_password", required=1))
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
        )
        if current_user.is_authenticated or session.get("admin_authenticated"):
            response.headers.setdefault("Cache-Control", "no-store, private")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.context_processor
    def template_globals():
        return {
            "app_name": app.config.get("APP_NAME", "TT Generator"),
            "app_version": app.config.get("APP_VERSION", "1.0.0"),
            "asset_version": app.config.get("ASSET_VERSION", "1"),
            "today": date.today(),
            "admin_authenticated": bool(session.get("admin_authenticated")),
        }

    @app.template_filter("duration")
    def duration_filter(minutes):
        try:
            minutes = int(minutes or 0)
        except (TypeError, ValueError):
            return "0m"
        hours, mins = divmod(minutes, 60)
        if hours and mins:
            return f"{hours}h {mins}m"
        if hours:
            return f"{hours}h"
        return f"{mins}m"

    @app.template_filter("datefmt")
    def datefmt_filter(value, fmt="%a, %d %b %Y"):
        if value is None:
            return ""
        try:
            return value.strftime(fmt)
        except AttributeError:
            return str(value)

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            if current_user.must_change_password:
                return redirect(url_for("auth.change_password"))
            if not current_user.onboarding_complete:
                return redirect(url_for("student.onboarding"))
            return redirect(url_for("student.dashboard"))
        return redirect(url_for("auth.login"))

    @app.get("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:
            db.session.rollback()
            return jsonify({"status": "unhealthy", "database": "unavailable"}), 503
        return jsonify({"status": "ok", "app": "tt-generator"})

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("errors/400.html"), 400

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(429)
    def rate_limited(error):
        return render_template("errors/429.html"), 429

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        app.logger.warning("CSRF validation failed on %s", request.path)
        return render_template("errors/csrf.html"), 400

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        app.logger.exception("Unhandled application error")
        return render_template("errors/500.html"), 500

    @app.errorhandler(Exception)
    def unhandled_exception(error):
        from werkzeug.exceptions import HTTPException

        if isinstance(error, HTTPException):
            return error
        if app.config.get("TESTING"):
            raise error
        db.session.rollback()
        app.logger.exception("Unhandled exception")
        return render_template("errors/500.html"), 500

    register_cli(app)
    return app


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-syllabus")
    @click.option("--file", "file_path", default=None, help="Path to a syllabus JSON file.")
    def seed_syllabus_command(file_path):
        """Import the built-in draft (or supplied) syllabus."""
        from app.syllabus.loader import seed_default_syllabus

        try:
            version = seed_default_syllabus(file_path)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Imported {version.board} {version.academic_year} {version.version_label} "
            f"({version.status}); {version.chapter_count} chapters."
        )

    @app.cli.command("validate-syllabus")
    @click.option("--file", "file_path", required=True, help="Path to a syllabus JSON file.")
    def validate_syllabus_command(file_path):
        """Validate a syllabus JSON file without importing it."""
        from app.syllabus.loader import read_syllabus_payload
        from app.syllabus.validator import validate_syllabus_payload

        try:
            payload = read_syllabus_payload(file_path)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        report = validate_syllabus_payload(payload)
        for warning in report["warnings"]:
            click.echo(f"WARNING: {warning}")
        for error in report["errors"]:
            click.echo(f"ERROR: {error}")
        click.echo(
            f"Valid={report['valid']} subjects={report.get('subject_count', 0)} "
            f"chapters={report.get('chapter_count', 0)} topics={report.get('topic_count', 0)}"
        )
        if not report["valid"]:
            raise click.ClickException("Syllabus validation failed.")

    @app.cli.command("check-health")
    def check_health():
        """Check database connectivity and configuration readiness."""
        from app.models import SyllabusVersion

        try:
            db.session.execute(text("SELECT 1"))
        except Exception as exc:
            raise click.ClickException(f"Database unavailable: {exc}") from exc
        versions = SyllabusVersion.query.filter_by(is_current=True).count()
        click.echo(f"Database: ok; current syllabus versions: {versions}")
