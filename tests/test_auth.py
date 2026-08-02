"""
Stage 2's critical path: registration, login, session cookies, logout,
and the two role-check dependencies. Split into two layers deliberately:

- Unit tests against app.auth.service directly (no HTTP) for the exact
  business rules: duplicate email, wrong password, role enforcement.
- HTTP tests through the real routes for the things that only exist at
  that layer: CSRF, cookie issuance, redirects, status codes.
"""

import pytest

from app.auth import service
from app.core.exceptions import NotAuthenticated, NotAuthorized
from app.core.security import hash_password, verify_password


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def test_hash_password_does_not_store_plaintext():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"


def test_verify_password_round_trip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True
    assert verify_password("wrong password", hashed) is False


def test_verify_password_treats_a_malformed_hash_as_no_match():
    """Stage 17 coverage gap: a corrupted/truncated hash in the DB
    (bad migration, manual data fix gone wrong, ...) must fail closed
    — "doesn't match" — rather than crashing verify_password with a
    500 the login form has no way to explain to the user."""
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


# ---------------------------------------------------------------------------
# app.auth.service — unit level, no HTTP
# ---------------------------------------------------------------------------

def test_register_user_creates_user_with_default_role(db_session):
    user = service.register_user(
        db_session, email="Ada@Example.com", password="password123", display_name="Ada"
    )
    assert user.id is not None
    assert user.email == "ada@example.com"  # normalized to lowercase
    assert user.role == "user"
    assert user.password_hash != "password123"


def test_register_user_rejects_duplicate_email(db_session):
    service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    with pytest.raises(service.EmailAlreadyRegistered):
        service.register_user(
            db_session, email="ADA@example.com", password="somethingelse", display_name="Ada 2"
        )


def test_authenticate_user_succeeds_with_correct_password(db_session):
    service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    user = service.authenticate_user(db_session, email="ada@example.com", password="password123")
    assert user.email == "ada@example.com"


def test_authenticate_user_rejects_wrong_password(db_session):
    service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    with pytest.raises(service.InvalidCredentials):
        service.authenticate_user(db_session, email="ada@example.com", password="wrong-password")


def test_authenticate_user_rejects_unknown_email(db_session):
    with pytest.raises(service.InvalidCredentials):
        service.authenticate_user(db_session, email="nobody@example.com", password="password123")


def test_session_lifecycle(db_session):
    user = service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    session = service.create_session(db_session, user)

    fetched = service.get_user_by_session_token(db_session, session.token)
    assert fetched is not None
    assert fetched.id == user.id

    service.delete_session(db_session, session.token)
    assert service.get_user_by_session_token(db_session, session.token) is None


def test_get_user_by_session_token_rejects_expired_session(db_session):
    from datetime import timedelta

    from app.core.time import utcnow
    from app.db.models.auth_session import AuthSession

    user = service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    expired = AuthSession(token="expired-token", user_id=user.id, expires_at=utcnow() - timedelta(days=1))
    db_session.add(expired)
    db_session.commit()

    assert service.get_user_by_session_token(db_session, "expired-token") is None


# ---------------------------------------------------------------------------
# Role-check dependencies
# ---------------------------------------------------------------------------

def test_require_admin_rejects_regular_user(db_session):
    from app.auth.dependencies import require_admin

    user = service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    with pytest.raises(NotAuthorized):
        require_admin(user)


def test_require_admin_allows_admin(db_session):
    from app.auth.dependencies import require_admin

    admin = service.register_user(
        db_session, email="admin@example.com", password="password123", display_name="Admin", role="admin"
    )
    assert require_admin(admin) is admin


def test_require_user_rejects_none():
    from app.auth.dependencies import require_user

    with pytest.raises(NotAuthenticated):
        require_user(None)


# ---------------------------------------------------------------------------
# HTTP layer — full request/response cycle
# ---------------------------------------------------------------------------

def _csrf_token_from(response):
    return response.cookies.get("csrf_token")


def test_register_then_dashboard_requires_no_further_login(client):
    form_response = client.get("/register")
    csrf = _csrf_token_from(form_response)

    response = client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Ada Lovelace",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert "session_token" in response.cookies

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Ada Lovelace" in dashboard.text


def test_register_rejects_mismatched_csrf(client):
    client.get("/register")  # mints the real csrf cookie, which we ignore below
    response = client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Ada",
            "csrf_token": "not-the-real-token",
        },
    )
    assert response.status_code == 422
    assert "session_token" not in response.cookies


def test_register_rejects_short_password(client):
    form_response = client.get("/register")
    csrf = _csrf_token_from(form_response)
    response = client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "short",
            "confirm_password": "short",
            "display_name": "Ada",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 422
    assert "at least 8 characters" in response.text


def test_register_rejects_duplicate_email_over_http(client):
    csrf = _csrf_token_from(client.get("/register"))
    payload = {
        "email": "ada@example.com",
        "password": "password123",
        "confirm_password": "password123",
        "display_name": "Ada",
        "csrf_token": csrf,
    }
    client.post("/register", data=payload)

    csrf2 = _csrf_token_from(client.get("/register"))
    payload["csrf_token"] = csrf2
    response = client.post("/register", data=payload)
    assert response.status_code == 422
    assert "already exists" in response.text


def test_login_success_sets_session_cookie(client):
    csrf = _csrf_token_from(client.get("/register"))
    client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Ada",
            "csrf_token": csrf,
        },
    )
    client.cookies.delete("session_token")

    login_csrf = _csrf_token_from(client.get("/login"))
    response = client.post(
        "/login",
        data={"email": "ada@example.com", "password": "password123", "csrf_token": login_csrf},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_login_rejects_wrong_password(client):
    csrf = _csrf_token_from(client.get("/register"))
    client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Ada",
            "csrf_token": csrf,
        },
    )
    client.cookies.delete("session_token")

    login_csrf = _csrf_token_from(client.get("/login"))
    response = client.post(
        "/login",
        data={"email": "ada@example.com", "password": "wrong-password", "csrf_token": login_csrf},
    )
    assert response.status_code == 422
    assert "Incorrect email or password" in response.text


def test_dashboard_redirects_when_not_logged_in(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_logout_clears_session(client):
    csrf = _csrf_token_from(client.get("/register"))
    client.post(
        "/register",
        data={
            "email": "ada@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Ada",
            "csrf_token": csrf,
        },
    )

    logout_response = client.post("/logout", follow_redirects=False)
    assert logout_response.status_code == 303

    dashboard = client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == "/login"
