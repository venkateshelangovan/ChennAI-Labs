"""
Stage 12's actual payoff, tested end to end: app/recommendations/cache.py
must serve a fresh RecommendationSnapshot without calling back into
Stage 8's generate_recommendations or Stage 10's generate_narration at
all, and must call both when the trigger says to regenerate. Call
counts are the point here, not just final content, so every test
monkeypatches cache.generate_recommendations / cache.generate_narration
with counting wrappers around the real functions — the real pipeline
still runs when it's supposed to, this just proves how many times.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.db.models.user_event import UserEvent
from app.products import service as product_service
from app.recommendations import cache as reco_cache
from app.recommendations import trigger

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


def _insert_event(db, *, user_id, product_id=None, event_type="view", created_at=None):
    event = UserEvent(
        user_id=user_id,
        session_id="s",
        event_type=event_type,
        product_id=product_id,
        event_metadata={},
        client_event_id=str(uuid.uuid4()),
        created_at=created_at or NOW,
    )
    db.add(event)
    db.commit()
    return event


def _count_calls(monkeypatch, target_module, attr_name):
    real_fn = getattr(target_module, attr_name)
    calls = {"count": 0}

    def wrapper(*args, **kwargs):
        calls["count"] += 1
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(target_module, attr_name, wrapper)
    return calls


def test_first_call_is_a_cache_miss_and_persists_a_snapshot(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")
    narration_calls = _count_calls(monkeypatch, reco_cache, "generate_narration")

    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    assert outcome.cache_hit is False
    assert outcome.trigger_reason == "no_snapshot"
    assert reco_calls["count"] == 1
    assert narration_calls["count"] == 1

    snapshot = db_session.query(RecommendationSnapshot).filter_by(user_id=1).one()
    assert snapshot.strategy == outcome.result.strategy
    assert len(snapshot.recommendations) == len(outcome.result.recommendations)


def test_second_call_within_ttl_is_a_cache_hit_and_skips_generation(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)  # seed a snapshot

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")
    narration_calls = _count_calls(monkeypatch, reco_cache, "generate_narration")

    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW + timedelta(minutes=1))

    assert outcome.cache_hit is True
    assert outcome.trigger_reason == "fresh"
    assert reco_calls["count"] == 0
    assert narration_calls["count"] == 0
    assert len(outcome.result.recommendations) == 1
    assert outcome.result.recommendations[0].product.title == "Only Course"


def test_stale_snapshot_with_signal_regenerates(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    for i in range(trigger.MIN_EVENTS_SINCE_REFRESH):
        _insert_event(db_session, user_id=1, created_at=NOW + timedelta(hours=1, minutes=i))

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")

    later = NOW + timedelta(hours=trigger.TTL_HOURS + 2)
    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=later)

    assert outcome.cache_hit is False
    assert outcome.trigger_reason == "ttl_and_signal"
    assert reco_calls["count"] == 1


def test_stale_snapshot_without_signal_still_serves_cache(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")

    later = NOW + timedelta(hours=trigger.TTL_HOURS + 2)  # stale, but no new events
    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=later)

    assert outcome.cache_hit is True
    assert outcome.trigger_reason == "stale_but_no_signal"
    assert reco_calls["count"] == 0


def test_manual_refresh_bypasses_a_fresh_ttl(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")

    later = NOW + timedelta(seconds=trigger.MANUAL_REFRESH_COOLDOWN_SECONDS + 5)
    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, manual_refresh=True, now=later)

    assert outcome.cache_hit is False
    assert outcome.trigger_reason == "manual_refresh"
    assert reco_calls["count"] == 1


def test_manual_refresh_on_cooldown_serves_cache_instead(db_session, monkeypatch):
    _make_product(db_session, title="Only Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")

    later = NOW + timedelta(seconds=trigger.MANUAL_REFRESH_COOLDOWN_SECONDS - 5)
    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, manual_refresh=True, now=later)

    assert outcome.cache_hit is True
    assert outcome.trigger_reason == "manual_refresh_on_cooldown"
    assert reco_calls["count"] == 0


def test_cached_recommendation_re_fetches_live_product_fields(db_session):
    product = _make_product(db_session, title="Only Course", price=Decimal("999"))
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)

    product.price = Decimal("1499")
    db_session.commit()

    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW + timedelta(minutes=1))

    assert outcome.cache_hit is True
    assert outcome.result.recommendations[0].product.price == Decimal("1499")  # live, not the cached price


def test_all_cached_products_archived_falls_back_to_regenerating(db_session, monkeypatch):
    solo = _make_product(db_session, title="Solo Course")
    reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW)  # snapshot recommends `solo`

    product_service.archive_product(db_session, solo)
    _make_product(db_session, title="Replacement Course")  # something else must exist to recommend

    reco_calls = _count_calls(monkeypatch, reco_cache, "generate_recommendations")

    outcome = reco_cache.get_dashboard_recommendations(db_session, user_id=1, now=NOW + timedelta(minutes=1))

    assert outcome.cache_hit is False
    assert outcome.trigger_reason == "cached_products_gone"
    assert reco_calls["count"] == 1


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_dashboard_shows_cache_banner_on_second_view(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="cache-demo@example.com", password="password123", display_name="Cache Demo"
    )
    _make_product(db_session, title="Cache Demo Course")
    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    first = client.get("/dashboard")
    second = client.get("/dashboard")

    assert first.status_code == 200
    assert "Freshly generated just now" in first.text
    assert second.status_code == 200
    assert "no AI calls made for this page view" in second.text


def test_manual_refresh_endpoint_redirects_to_dashboard(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="refresh-demo@example.com", password="password123", display_name="Refresh Demo"
    )
    _make_product(db_session, title="Refresh Demo Course")
    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    dashboard_page = client.get("/dashboard")
    token = dashboard_page.text.split('name="csrf_token" value="')[1].split('"')[0]

    response = client.post("/dashboard/refresh", data={"csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
