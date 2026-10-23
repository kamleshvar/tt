from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from app.models import User
from app.security import password_problem
from extensions import db


def registration_data(email: str = "new.student@example.com", **overrides):
    data = {
        "full_name": "New Student",
        "email": email,
        "password": "SecurePass123",
        "confirm_password": "SecurePass123",
        "student_class": "Class 12",
        "board": "CBSE",
        "academic_year": "2026-27",
    }
    data.update(overrides)
    return data


def test_registration_creates_student_and_logs_in(client, app):
    response = client.post("/register", data=registration_data("New.Student@Example.com"))
    assert response.status_code == 302
    assert "/onboarding" in response.headers["Location"]

    with app.app_context():
        user = User.query.one()
        assert user.email == "new.student@example.com"
        assert user.full_name == "New Student"
        assert user.profile is not None
        assert user.profile.onboarding_completed is False
        assert user.check_password("SecurePass123")


def test_registration_rejects_duplicate_email(client, student_factory):
    student_factory(email="taken@example.com")

    response = client.post("/register", data=registration_data("TAKEN@example.com"))

    assert response.status_code == 400
    assert b"already exists" in response.data


def test_registration_rejects_weak_password(client, app):
    response = client.post(
        "/register",
        data=registration_data(
            password="alllowercase",
            confirm_password="alllowercase",
        ),
    )

    assert response.status_code == 400
    assert b"number" in response.data.lower()
    with app.app_context():
        assert User.query.count() == 0
    assert password_problem("alllowercase") is not None


def test_registration_rejects_mismatched_passwords(client, app):
    response = client.post(
        "/register",
        data=registration_data(confirm_password="SecurePass124"),
    )

    assert response.status_code == 200
    with app.app_context():
        assert User.query.count() == 0


def test_login_logout_and_inactive_student(client, app, student_factory):
    user_id = student_factory(email="login@example.com", onboarded=False)

    bad = client.post(
        "/login",
        data={"email": "login@example.com", "password": "WrongPass123"},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/login",
        data={"email": "login@example.com", "password": "StudentPass123"},
    )
    assert ok.status_code == 302
    assert "/onboarding" in ok.headers["Location"]

    dashboard = client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code == 302
    assert "/onboarding" in dashboard.headers["Location"]

    assert client.post("/logout").status_code == 302

    with app.app_context():
        user = db.session.get(User, user_id)
        user.is_active = False
        db.session.commit()

    blocked = client.post(
        "/login",
        data={"email": "login@example.com", "password": "StudentPass123"},
    )
    assert blocked.status_code == 403
    assert b"deactivated" in blocked.data.lower()


def test_login_rejects_unsafe_next_url(client, student_factory):
    student_factory(email="safe@example.com")

    response = client.post(
        "/login?next=https://evil.example/phish",
        data={"email": "safe@example.com", "password": "StudentPass123"},
    )

    assert response.status_code == 302
    location = response.headers["Location"]
    parsed = urlparse(location)
    assert parsed.netloc == ""
    assert parsed.path == "/dashboard"

    assert client.post("/logout").status_code == 302

    response = client.post(
        "/login?next=/profile",
        data={"email": "safe@example.com", "password": "StudentPass123"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/profile")


def test_protected_routes_redirect_anonymous_users(client):
    for path in ("/dashboard", "/planner/week", "/subjects", "/profile", "/recovery"):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]
        query = parse_qs(urlparse(response.headers["Location"]).query)
        assert query["next"][0] == path


def test_public_pages_use_full_width_shell(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert b'class="app-shell app-shell-public"' in response.data
    assert b"/static/css/app.css?v=" in response.data


def test_authenticated_pages_use_sidebar_shell(client, student_factory):
    student_factory(email="layout@example.com", onboarded=True)
    client.post(
        "/login",
        data={"email": "layout@example.com", "password": "StudentPass123"},
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'class="app-shell"' in response.data
    assert b'class="app-shell app-shell-public"' not in response.data
    assert b'class="sidebar"' in response.data


def test_login_with_csrf_enabled(client, app, student_factory):
    student_factory(email="csrf@example.com")
    app.config["WTF_CSRF_ENABLED"] = True

    page = client.get("/login")
    match = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', page.data)
    assert match is not None

    response = client.post(
        "/login",
        data={
            "email": "csrf@example.com",
            "password": "StudentPass123",
            "csrf_token": match.group(1).decode(),
        },
    )

    assert response.status_code == 302
