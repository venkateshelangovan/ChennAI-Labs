"""
Stage 6's critical path: the deterministic math in app/profile/service.py
(event-type weighting, recency decay, time_spent capping, category/tag
normalization, per-user isolation) and that /profile actually renders it
and is gated like every other logged-in page.

Most tests insert UserEvent rows directly rather than going through
POST /api/events, because the decay/recency assertions need exact
control over `created_at` — something a live HTTP request can't give us
without freezing the clock. `_insert_event` is the raw equivalent of
what app/events/service.ingest_events would have stored.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.user_event import UserEvent
from app.products import service as product_service
from app.profile import service as profile_service

FIXED_NOW = utcnow()


def _make_product(db, **overrides):
    defaults = dict(
        title="DSA for MAANG Interviews",
        description="Arrays, trees, graphs, and the interview patterns behind them.",
        category="DSA / Interview Prep",
        subcategory="Core DSA",
        price=Decimal("4999"),
        level="intermediate",
        tags=["dsa", "interview-prep"],
        instructor="Priya Raman",
        duration_minutes=1200,
        image_url=None,
    )
    defaults.update(overrides)
    return product_service.create_product(db, **defaults)


def _insert_event(db, *, user_id, event_type, product_id=None, metadata=None, created_at=None, session_id="s"):
    event = UserEvent(
        user_id=user_id,
        session_id=session_id,
        event_type=event_type,
        product_id=product_id,
        event_metadata=metadata or {},
        client_event_id=str(uuid.uuid4()),
        created_at=created_at or FIXED_NOW,
    )
    db.add(event)
    db.commit()
    return event


# ---------------------------------------------------------------------------
# app.profile.service — unit level
# ---------------------------------------------------------------------------

def test_no_events_is_cold_start(db_session):
    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)
    assert profile.is_cold_start
    assert profile.sample_size == 0
    assert profile.category_scores == []
    assert profile.top_products == []


def test_single_view_gives_full_category_share(db_session):
    product = _make_product(db_session, category="DSA / Interview Prep")
    _insert_event(db_session, user_id=1, event_type="view", product_id=product.id, created_at=FIXED_NOW)

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    assert not profile.is_cold_start
    assert profile.sample_size == 1
    assert len(profile.category_scores) == 1
    assert profile.category_scores[0].category == "DSA / Interview Prep"
    assert profile.category_scores[0].score == 1.0
    assert profile.category_scores[0].evidence_count == 1
    assert len(profile.top_products) == 1
    assert profile.top_products[0].relative_score == 1.0
    assert profile.top_products[0].slug == product.slug


def test_click_is_weighted_more_than_view(db_session):
    """click (2.0) vs view (1.0) on same-age events -> a 2:1 raw ratio,
    which normalizes to 0.6667 / 0.3333 across the two categories."""
    clicked = _make_product(db_session, category="Clicked Category", title="Clicked Course")
    viewed = _make_product(db_session, category="Viewed Category", title="Viewed Course")
    _insert_event(db_session, user_id=1, event_type="click", product_id=clicked.id, created_at=FIXED_NOW)
    _insert_event(db_session, user_id=1, event_type="view", product_id=viewed.id, created_at=FIXED_NOW)

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    scores = {c.category: c.score for c in profile.category_scores}
    assert abs(scores["Clicked Category"] - 2 / 3) < 0.001
    assert abs(scores["Viewed Category"] - 1 / 3) < 0.001


def test_recency_decay_halves_at_one_half_life(db_session):
    recent = _make_product(db_session, category="Recent Category", title="Recent Course")
    old = _make_product(db_session, category="Old Category", title="Old Course")
    _insert_event(db_session, user_id=1, event_type="view", product_id=recent.id, created_at=FIXED_NOW)
    _insert_event(
        db_session,
        user_id=1,
        event_type="view",
        product_id=old.id,
        created_at=FIXED_NOW - timedelta(days=profile_service.HALF_LIFE_DAYS),
    )

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)
    scores = {c.category: c.score for c in profile.category_scores}

    # raw weights: recent=1.0, old=0.5 -> normalized 0.6667 / 0.3333
    assert abs(scores["Recent Category"] - scores["Old Category"] * 2) < 0.005


def test_time_spent_weight_is_capped(db_session):
    over_cap = _make_product(db_session, category="Over Cap", title="Over Cap Course")
    at_cap = _make_product(db_session, category="At Cap", title="At Cap Course")
    _insert_event(
        db_session,
        user_id=1,
        event_type="time_spent",
        product_id=over_cap.id,
        metadata={"duration_ms": profile_service.TIME_SPENT_CAP_MS * 4},
        created_at=FIXED_NOW,
    )
    _insert_event(
        db_session,
        user_id=1,
        event_type="time_spent",
        product_id=at_cap.id,
        metadata={"duration_ms": profile_service.TIME_SPENT_CAP_MS},
        created_at=FIXED_NOW,
    )

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)
    scores = {c.category: c.score for c in profile.category_scores}

    assert abs(scores["Over Cap"] - scores["At Cap"]) < 0.0001
    assert abs(scores["Over Cap"] - 0.5) < 0.0001


def test_search_events_do_not_contribute_category_or_tag_weight(db_session):
    _insert_event(
        db_session, user_id=1, event_type="search", metadata={"query": "reinforcement learning"}, created_at=FIXED_NOW
    )

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    assert profile.sample_size == 1
    assert profile.category_scores == []
    assert profile.tag_scores == []
    assert len(profile.search_terms) == 1
    assert profile.search_terms[0].query == "reinforcement learning"
    assert profile.search_terms[0].count == 1


def test_search_terms_dedupe_case_insensitively_and_count(db_session):
    older = FIXED_NOW - timedelta(days=1)
    newer = FIXED_NOW
    _insert_event(db_session, user_id=1, event_type="search", metadata={"query": "rag systems"}, created_at=older)
    _insert_event(db_session, user_id=1, event_type="search", metadata={"query": "RAG Systems"}, created_at=newer)

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    assert len(profile.search_terms) == 1
    term = profile.search_terms[0]
    assert term.count == 2
    # the newest occurrence's casing wins, since events are processed newest-first
    assert term.query == "RAG Systems"
    assert term.last_searched_at == newer


def test_product_tags_split_weight_evenly(db_session):
    product = _make_product(db_session, tags=["python", "dsa"])
    _insert_event(db_session, user_id=1, event_type="view", product_id=product.id, created_at=FIXED_NOW)

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    tag_scores = {t.tag: t.score for t in profile.tag_scores}
    assert tag_scores == {"python": 0.5, "dsa": 0.5}


def test_top_products_capped_at_limit(db_session):
    for i in range(profile_service.TOP_PRODUCTS_LIMIT + 2):
        product = _make_product(db_session, title=f"Course {i}", category=f"Category {i}")
        _insert_event(db_session, user_id=1, event_type="view", product_id=product.id, created_at=FIXED_NOW)

    profile = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    assert len(profile.top_products) == profile_service.TOP_PRODUCTS_LIMIT


def test_profile_only_reflects_this_users_events(db_session):
    product_a = _make_product(db_session, category="User A Category", title="A's course")
    product_b = _make_product(db_session, category="User B Category", title="B's course")
    _insert_event(db_session, user_id=1, event_type="view", product_id=product_a.id, created_at=FIXED_NOW)
    _insert_event(db_session, user_id=2, event_type="view", product_id=product_b.id, created_at=FIXED_NOW)

    profile_a = profile_service.build_interest_profile(db_session, user_id=1, now=FIXED_NOW)

    assert profile_a.sample_size == 1
    assert [c.category for c in profile_a.category_scores] == ["User A Category"]


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_profile_page_requires_login(client):
    response = client.get("/profile", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_profile_page_shows_cold_start_message_for_new_user(client, db_session):
    csrf = client.get("/register").cookies.get("csrf_token")
    client.post(
        "/register",
        data={
            "email": "freshlearner@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Fresh Learner",
            "csrf_token": csrf,
        },
    )
    response = client.get("/profile")
    assert response.status_code == 200
    assert "Nothing here yet" in response.text


def test_profile_page_renders_category_scores(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="engaged@example.com", password="password123", display_name="Engaged Learner"
    )
    product = _make_product(db_session, category="Agentic AI")
    _insert_event(db_session, user_id=user.id, event_type="view", product_id=product.id, created_at=FIXED_NOW)

    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    response = client.get("/profile")
    assert response.status_code == 200
    assert "Agentic AI" in response.text
    assert product.title in response.text
