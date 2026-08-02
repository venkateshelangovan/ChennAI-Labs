"""
Stage 15 (bonus): the proactive digest. Two layers:

1. `run_daily_digest` itself — only regenerates users who already have
   a snapshot, isolates per-user failures, tags every write with
   `trigger_reason="scheduled_digest"`, and returns an accurate summary.
2. The admin "Run digest now" action (app/admin/recommendations_routes.py)
   — same CSRF gating and access control as every other admin POST,
   calling the exact same function the scheduler calls.

app/core/scheduler.py's APScheduler wiring itself is intentionally not
unit tested here — there is nothing to assert about a cron trigger
firing at settings.digest_hour_utc beyond "APScheduler is configured
correctly," which is exactly what live verification (starting the real
app and checking it doesn't crash on startup/shutdown) covers instead.
"""

import uuid
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.db.models.user_event import UserEvent
from app.products import service as product_service
from app.recommendations import cache as reco_cache
from app.recommendations import digest as digest_module

NOW = utcnow()


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
# run_daily_digest
# ---------------------------------------------------------------------------

def test_digest_skips_users_with_no_snapshot(db_session):
    from app.auth import service as auth_service

    auth_service.register_user(db_session, email="nosnap@example.com", password="password123", display_name="No Snap")
    _make_product(db_session, title="Only Course")

    summary = digest_module.run_daily_digest(db_session, now=NOW)

    assert summary.total_users == 0
    assert summary.succeeded == 0
    assert summary.failed == 0


def test_digest_regenerates_every_user_with_an_existing_snapshot(db_session):
    from app.auth import service as auth_service

    a = auth_service.register_user(db_session, email="a@example.com", password="password123", display_name="A")
    b = auth_service.register_user(db_session, email="b@example.com", password="password123", display_name="B")
    _make_product(db_session, title="Only Course")

    reco_cache.get_dashboard_recommendations(db_session, a.id, now=NOW)  # seed snapshots
    reco_cache.get_dashboard_recommendations(db_session, b.id, now=NOW)

    summary = digest_module.run_daily_digest(db_session, now=NOW)

    assert summary.total_users == 2
    assert summary.succeeded == 2
    assert summary.failed == 0

    for user_id in (a.id, b.id):
        snapshot = db_session.query(RecommendationSnapshot).filter_by(user_id=user_id).one()
        assert snapshot.trigger_reason == "scheduled_digest"
        assert snapshot.generated_at == NOW


def test_digest_updates_the_same_snapshot_row_not_a_new_one(db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(db_session, email="c@example.com", password="password123", display_name="C")
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user.id, now=NOW)

    digest_module.run_daily_digest(db_session, now=NOW)

    rows = db_session.query(RecommendationSnapshot).filter_by(user_id=user.id).all()
    assert len(rows) == 1


def test_digest_isolates_a_single_user_failure(db_session, monkeypatch):
    from app.auth import service as auth_service

    good = auth_service.register_user(db_session, email="good@example.com", password="password123", display_name="Good")
    bad = auth_service.register_user(db_session, email="bad@example.com", password="password123", display_name="Bad")
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, good.id, now=NOW)
    reco_cache.get_dashboard_recommendations(db_session, bad.id, now=NOW)

    real_fn = digest_module.regenerate_and_persist

    def flaky(db, user_id, *, trigger_reason, now=None):
        if user_id == bad.id:
            raise RuntimeError("simulated Mesh outage for this user only")
        return real_fn(db, user_id, trigger_reason=trigger_reason, now=now)

    monkeypatch.setattr(digest_module, "regenerate_and_persist", flaky)

    summary = digest_module.run_daily_digest(db_session, now=NOW)

    assert summary.total_users == 2
    assert summary.succeeded == 1
    assert summary.failed == 1
    assert summary.failed_user_ids == [bad.id]

    # the good user's snapshot still updated despite the other user's failure
    good_snapshot = db_session.query(RecommendationSnapshot).filter_by(user_id=good.id).one()
    assert good_snapshot.trigger_reason == "scheduled_digest"


# ---------------------------------------------------------------------------
# Admin "Run digest now"
# ---------------------------------------------------------------------------

def test_run_digest_now_requires_admin(client):
    response = client.post("/admin/recommendations/run-digest", data={"csrf_token": "x"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_run_digest_now_regenerates_snapshots_and_redirects_with_result(client, db_session):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="learner@example.com", password="password123", display_name="Learner"
    )
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, learner.id, now=NOW)

    _login(client, db_session, admin)
    list_page = client.get("/admin/recommendations")
    token = list_page.text.split('name="csrf_token" value="')[1].split('"')[0]

    response = client.post("/admin/recommendations/run-digest", data={"csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/recommendations?digest_result=1/1%20regenerated"

    snapshot = db_session.query(RecommendationSnapshot).filter_by(user_id=learner.id).one()
    assert snapshot.trigger_reason == "scheduled_digest"


def test_run_digest_now_bad_csrf_does_not_regenerate(client, db_session):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="learner2@example.com", password="password123", display_name="Learner Two"
    )
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, learner.id, now=NOW)

    _login(client, db_session, admin)
    response = client.post(
        "/admin/recommendations/run-digest", data={"csrf_token": "not-the-real-token"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert "csrf_failed" in response.headers["location"]

    snapshot = db_session.query(RecommendationSnapshot).filter_by(user_id=learner.id).one()
    assert snapshot.trigger_reason == "no_snapshot"  # unchanged — digest never ran
