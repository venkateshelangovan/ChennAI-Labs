"""
Stage 16: production hardening, tested at the HTTP layer where it
actually matters — a route wiring `rate_limit.allow` incorrectly (wrong
bucket, wrong identifier, or just forgetting to check the return value)
wouldn't be caught by tests/test_rate_limit.py, which only tests the
counting primitive in isolation.

Covers: login/register lockout after repeated attempts, dashboard/
refresh and admin run-digest hitting their rate limits, the security
headers middleware, and the cookie `secure` flag actually flipping with
`settings.is_production`.
"""

from decimal import Decimal

from app.auth import routes as auth_routes
from app.admin import recommendations_routes as admin_reco_routes
from app.core.config import settings
from app.products import service as product_service
from app.recommendations import cache as reco_cache


def _csrf_token_from(response):
    return response.cookies.get("csrf_token")


def _register(client, email="ada@example.com", password="password123", display_name="Ada"):
    csrf = _csrf_token_from(client.get("/register"))
    return client.post(
        "/register",
        data={
            "email": email,
            "password": password,
            "confirm_password": password,
            "display_name": display_name,
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )


def _make_product(db, **overrides):
    defaults = dict(
        title="Robotics Engineering Fundamentals",
        description="Sensors, actuators, and control loops.",
        category="Robotics Engineering",
        subcategory=None,
        price=Decimal("4999"),
        level="intermediate",
        tags=["robotics"],
        instructor="Someone",
        duration_minutes=1000,
        image_url=None,
    )
    defaults.update(overrides)
    return product_service.create_product(db, **defaults)


def _register_admin(db_session, email="admin@example.com"):
    from app.auth import service as auth_service

    return auth_service.register_user(
        db_session, email=email, password="password123", display_name="Admin", role="admin"
    )


def _login(client, db_session, user):
    from app.auth import service as auth_service

    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)


# ---------------------------------------------------------------------------
# Login / register lockout
# ---------------------------------------------------------------------------

def test_register_locks_out_after_the_rate_limit(client, monkeypatch):
    monkeypatch.setitem(auth_routes.REGISTER_RATE_LIMIT, "limit", 2)

    _register(client, email="one@example.com")
    _register(client, email="two@example.com")
    response = _register(client, email="three@example.com")

    assert response.status_code == 422
    assert "Too many registration attempts" in response.text


def test_login_locks_out_after_the_rate_limit(client, db_session, monkeypatch):
    from app.auth import service as auth_service

    auth_service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    monkeypatch.setitem(auth_routes.LOGIN_RATE_LIMIT, "limit", 2)

    def _attempt():
        csrf = _csrf_token_from(client.get("/login"))
        return client.post(
            "/login",
            data={"email": "ada@example.com", "password": "wrong-password", "csrf_token": csrf},
        )

    _attempt()
    _attempt()
    response = _attempt()

    assert response.status_code == 422
    assert "Too many login attempts" in response.text


def test_login_lockout_blocks_even_the_correct_password(client, db_session, monkeypatch):
    """The point of a login rate limit is that it doesn't care whether
    the Nth attempt would have succeeded — that's what makes it actually
    blunt brute force rather than just bad-password spam."""
    from app.auth import service as auth_service

    auth_service.register_user(db_session, email="ada@example.com", password="password123", display_name="Ada")
    monkeypatch.setitem(auth_routes.LOGIN_RATE_LIMIT, "limit", 1)

    csrf1 = _csrf_token_from(client.get("/login"))
    client.post("/login", data={"email": "ada@example.com", "password": "wrong-password", "csrf_token": csrf1})

    csrf2 = _csrf_token_from(client.get("/login"))
    response = client.post(
        "/login", data={"email": "ada@example.com", "password": "password123", "csrf_token": csrf2}
    )

    assert response.status_code == 422
    assert "Too many login attempts" in response.text
    assert "session_token" not in response.cookies


# ---------------------------------------------------------------------------
# Dashboard refresh rate limit
# ---------------------------------------------------------------------------

def test_dashboard_refresh_rate_limit_stops_regenerating(client, db_session, monkeypatch):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="ada@example.com", password="password123", display_name="Ada"
    )
    _make_product(db_session, title="Only Course")
    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    monkeypatch.setitem(auth_routes.REFRESH_RATE_LIMIT, "limit", 1)

    dashboard_page = client.get("/dashboard")
    csrf = dashboard_page.text.split('name="csrf_token" value="')[1].split('"')[0]

    calls = {"count": 0}
    real_get = reco_cache.get_dashboard_recommendations

    def counting_get(db, user_id, **kwargs):
        if kwargs.get("manual_refresh"):
            calls["count"] += 1
        return real_get(db, user_id, **kwargs)

    monkeypatch.setattr(auth_routes, "get_dashboard_recommendations", counting_get)

    client.post("/dashboard/refresh", data={"csrf_token": csrf}, follow_redirects=False)
    client.post("/dashboard/refresh", data={"csrf_token": csrf}, follow_redirects=False)

    assert calls["count"] == 1  # the second manual refresh was rate-limited, not just cooldown-blocked


# ---------------------------------------------------------------------------
# Admin "run digest now" rate limit
# ---------------------------------------------------------------------------

def test_run_digest_now_rate_limit(client, db_session, monkeypatch):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="learner@example.com", password="password123", display_name="Learner"
    )
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, learner.id)

    monkeypatch.setitem(admin_reco_routes.RUN_DIGEST_RATE_LIMIT, "limit", 1)
    _login(client, db_session, admin)

    list_page = client.get("/admin/recommendations")
    token = list_page.text.split('name="csrf_token" value="')[1].split('"')[0]

    first = client.post("/admin/recommendations/run-digest", data={"csrf_token": token}, follow_redirects=False)
    second = client.post("/admin/recommendations/run-digest", data={"csrf_token": token}, follow_redirects=False)

    assert "rate_limited" not in first.headers["location"]
    assert "rate_limited" in second.headers["location"]


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

def test_security_headers_present_on_every_response(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_hsts_absent_in_development(client):
    response = client.get("/health")
    assert "strict-transport-security" not in response.headers


def test_hsts_present_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    response = client.get("/health")
    assert "strict-transport-security" in response.headers


# ---------------------------------------------------------------------------
# Cookie `secure` flag
# ---------------------------------------------------------------------------

def test_csrf_cookie_not_secure_in_development(client):
    response = client.get("/register")
    set_cookie_headers = response.headers.get_list("set-cookie")
    csrf_header = next(h for h in set_cookie_headers if h.startswith("csrf_token="))
    assert "Secure" not in csrf_header


def test_csrf_cookie_secure_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    response = client.get("/register")
    set_cookie_headers = response.headers.get_list("set-cookie")
    csrf_header = next(h for h in set_cookie_headers if h.startswith("csrf_token="))
    assert "Secure" in csrf_header


def test_session_cookie_secure_in_production(client, monkeypatch, db_session):
    """
    A full register round trip, not just a single response's headers:
    the CSRF cookie itself is `Secure` once `app_env=production`
    (previous test), and httpx's cookie jar — correctly mimicking real
    browser behavior — won't re-attach a `Secure` cookie to a
    subsequent request against TestClient's plain-http base URL. So the
    CSRF token has to be threaded through explicitly here (its value
    read off the Set-Cookie header, then placed directly in the jar)
    rather than relying on the client to round-trip it automatically —
    that's a property of the test transport, not a real app bug.
    """
    monkeypatch.setattr(settings, "app_env", "production")

    form_response = client.get("/register")
    csrf_header = next(h for h in form_response.headers.get_list("set-cookie") if h.startswith("csrf_token="))
    csrf_value = csrf_header.split("csrf_token=", 1)[1].split(";", 1)[0]
    client.cookies.set("csrf_token", csrf_value)

    response = client.post(
        "/register",
        data={
            "email": "prod@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Prod",
            "csrf_token": csrf_value,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303  # registration actually succeeded
    set_cookie_headers = response.headers.get_list("set-cookie")
    session_header = next(h for h in set_cookie_headers if h.startswith("session_token="))
    assert "Secure" in session_header
