"""
Stage 12's decision logic in isolation: app/recommendations/trigger.py's
`evaluate()`. Every case here is deterministic and DB-only — no Mesh,
no vector store — matching the module's whole point (Section 14: zero
AI cost to decide whether to spend AI cost). A `RecommendationSnapshot`
is constructed directly (never persisted) since `evaluate()` only reads
its `generated_at` field and never queries the snapshot table itself.
"""

import uuid
from datetime import timedelta

from app.core.time import utcnow
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.db.models.user_event import UserEvent
from app.recommendations import trigger

NOW = utcnow()


def _snapshot(generated_at):
    return RecommendationSnapshot(
        user_id=1,
        generated_at=generated_at,
        strategy="personalized",
        retrieval_refined=False,
        recommendations=[],
        narration_text=None,
        narration_grounded=False,
        narration_fallback_reason=None,
    )


def _insert_event(db, *, event_type="view", created_at):
    event = UserEvent(
        user_id=1,
        session_id="s",
        event_type=event_type,
        product_id=None,
        event_metadata={},
        client_event_id=str(uuid.uuid4()),
        created_at=created_at,
    )
    db.add(event)
    db.commit()
    return event


def test_no_snapshot_always_refreshes(db_session):
    decision = trigger.evaluate(db_session, None, user_id=1, now=NOW)
    assert decision.should_refresh is True
    assert decision.reason == "no_snapshot"


def test_fresh_snapshot_is_served_from_cache(db_session):
    snapshot = _snapshot(NOW - timedelta(hours=1))
    decision = trigger.evaluate(db_session, snapshot, user_id=1, now=NOW)
    assert decision.should_refresh is False
    assert decision.reason == "fresh"


def test_stale_snapshot_with_no_new_events_stays_cached(db_session):
    snapshot = _snapshot(NOW - timedelta(hours=trigger.TTL_HOURS + 1))
    decision = trigger.evaluate(db_session, snapshot, user_id=1, now=NOW)
    assert decision.should_refresh is False
    assert decision.reason == "stale_but_no_signal"


def test_stale_snapshot_with_enough_events_refreshes(db_session):
    generated_at = NOW - timedelta(hours=trigger.TTL_HOURS + 1)
    for _ in range(trigger.MIN_EVENTS_SINCE_REFRESH):
        _insert_event(db_session, created_at=NOW - timedelta(minutes=1))

    snapshot = _snapshot(generated_at)
    decision = trigger.evaluate(db_session, snapshot, user_id=1, now=NOW)
    assert decision.should_refresh is True
    assert decision.reason == "ttl_and_signal"


def test_stale_snapshot_with_one_search_event_refreshes_even_below_the_count_threshold(db_session):
    generated_at = NOW - timedelta(hours=trigger.TTL_HOURS + 1)
    _insert_event(db_session, event_type="search", created_at=NOW - timedelta(minutes=1))

    snapshot = _snapshot(generated_at)
    decision = trigger.evaluate(db_session, snapshot, user_id=1, now=NOW)
    assert decision.should_refresh is True
    assert decision.reason == "ttl_and_signal"


def test_events_before_the_snapshot_dont_count_as_new_signal(db_session):
    generated_at = NOW - timedelta(hours=trigger.TTL_HOURS + 1)
    for _ in range(trigger.MIN_EVENTS_SINCE_REFRESH):
        _insert_event(db_session, created_at=generated_at - timedelta(days=1))  # all BEFORE the snapshot

    snapshot = _snapshot(generated_at)
    decision = trigger.evaluate(db_session, snapshot, user_id=1, now=NOW)
    assert decision.should_refresh is False
    assert decision.reason == "stale_but_no_signal"


def test_manual_refresh_bypasses_ttl_and_signal(db_session):
    snapshot = _snapshot(NOW - timedelta(minutes=5))  # well within TTL, no new events
    decision = trigger.evaluate(db_session, snapshot, user_id=1, manual=True, now=NOW)
    assert decision.should_refresh is True
    assert decision.reason == "manual_refresh"


def test_manual_refresh_blocked_during_cooldown(db_session):
    snapshot = _snapshot(NOW - timedelta(seconds=trigger.MANUAL_REFRESH_COOLDOWN_SECONDS - 5))
    decision = trigger.evaluate(db_session, snapshot, user_id=1, manual=True, now=NOW)
    assert decision.should_refresh is False
    assert decision.reason == "manual_refresh_on_cooldown"


def test_manual_refresh_allowed_once_cooldown_elapses(db_session):
    snapshot = _snapshot(NOW - timedelta(seconds=trigger.MANUAL_REFRESH_COOLDOWN_SECONDS + 5))
    decision = trigger.evaluate(db_session, snapshot, user_id=1, manual=True, now=NOW)
    assert decision.should_refresh is True
    assert decision.reason == "manual_refresh"
