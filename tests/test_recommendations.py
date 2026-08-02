"""
Stage 8's critical path: the business rules layered on top of Stage 7's
raw retrieval — novelty (don't recommend what the user already engaged
with), diversity (cap per-category dominance), and a deterministic
popularity fallback for cold-start users / all-candidates-engaged edge
cases. `_diversify` is tested directly with synthetic data (fast,
exact control over category distribution); `generate_recommendations`
is tested end to end against the real (test-isolated) vector store,
using catalogs small enough that every product is guaranteed to be a
retrieval candidate — so these tests don't depend on the placeholder
embedder's exact similarity ordering, only on the business rules.
"""

import uuid
from collections import Counter
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.user_event import UserEvent
from app.products import service as product_service
from app.recommendations import service as reco_service

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


def _insert_view_event(db, *, user_id, product_id, created_at=None):
    event = UserEvent(
        user_id=user_id,
        session_id="s",
        event_type="view",
        product_id=product_id,
        event_metadata={},
        client_event_id=str(uuid.uuid4()),
        created_at=created_at or NOW,
    )
    db.add(event)
    db.commit()
    return event


# ---------------------------------------------------------------------------
# _diversify — pure function, synthetic data
# ---------------------------------------------------------------------------

def _item(label, category):
    return (label, category)


def test_diversify_respects_category_cap():
    cap = reco_service.MAX_PER_CATEGORY
    items = [_item(f"a{i}", "A") for i in range(cap + 2)] + [_item("b1", "B")]

    result = reco_service._diversify(items, top_n=cap + 1, category_of=lambda i: i[1])

    labels = [r[0] for r in result]
    assert Counter(r[1] for r in result)["A"] == cap
    assert "b1" in labels
    assert len(result) == cap + 1


def test_diversify_relaxes_cap_when_not_enough_other_categories():
    cap = reco_service.MAX_PER_CATEGORY
    items = [_item(f"a{i}", "A") for i in range(5)]  # only one category available

    result = reco_service._diversify(items, top_n=4, category_of=lambda i: i[1])

    assert len(result) == 4
    assert Counter(r[1] for r in result)["A"] == 4  # cap relaxed — no other category to fill with


def test_diversify_returns_everything_when_top_n_exceeds_pool():
    items = [_item("a1", "A"), _item("b1", "B")]
    result = reco_service._diversify(items, top_n=10, category_of=lambda i: i[1])
    assert len(result) == 2


def test_diversify_top_n_zero_returns_empty():
    items = [_item("a1", "A")]
    assert reco_service._diversify(items, top_n=0, category_of=lambda i: i[1]) == []


# ---------------------------------------------------------------------------
# generate_recommendations — end to end
# ---------------------------------------------------------------------------

def test_cold_start_user_gets_popular_fallback(db_session):
    _make_product(db_session, title="High Rated", category="Robotics Engineering", rating=4.8)
    _make_product(db_session, title="Mid Rated", category="Culinary Arts", rating=4.0)
    _make_product(db_session, title="No Rating", category="Foundations", rating=None)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=3, now=NOW)

    assert result.strategy == "popular_fallback"
    titles = [r.product.title for r in result.recommendations]
    assert titles == ["High Rated", "Mid Rated", "No Rating"]  # rating desc, nulls last
    assert all(r.similarity is None for r in result.recommendations)
    assert all(r.reason == reco_service.POPULAR_FALLBACK_REASON for r in result.recommendations)


def test_personalized_excludes_already_engaged_product(db_session):
    engaged = _make_product(db_session, title="Already Viewed", category="Robotics Engineering")
    novel = _make_product(db_session, title="Not Yet Seen", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=6, now=NOW)

    product_ids = [r.product.id for r in result.recommendations]
    assert engaged.id not in product_ids
    assert novel.id in product_ids
    assert result.strategy == "personalized"


def test_personalized_falls_back_to_popular_when_all_candidates_already_engaged(db_session):
    only_product = _make_product(db_session, title="Only Course", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=1, product_id=only_product.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=6, now=NOW)

    assert result.strategy == "popular_fallback"
    # the popular fallback must ALSO exclude the already-engaged product,
    # not just the personalized path
    assert result.recommendations == []


def test_recommendation_reason_names_the_candidates_own_category(db_session):
    engaged = _make_product(db_session, title="Engaged Course", category="Robotics Engineering")
    other = _make_product(db_session, title="Culinary Basics", category="Culinary Arts")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=6, now=NOW)

    rec = next(r for r in result.recommendations if r.product.id == other.id)
    assert rec.reason == "Matches your recent interest in Culinary Arts"
    assert rec.similarity is not None


def test_recommendations_respect_diversity_cap_end_to_end(db_session):
    engaged = _make_product(db_session, title="R1 Engaged", category="Robotics Engineering")
    _make_product(db_session, title="R2", category="Robotics Engineering")
    _make_product(db_session, title="R3", category="Robotics Engineering")
    _make_product(db_session, title="C1", category="Culinary Arts")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=3, now=NOW)

    categories = [r.product.category for r in result.recommendations]
    assert len(result.recommendations) == 3
    assert Counter(categories)["Robotics Engineering"] == reco_service.MAX_PER_CATEGORY
    assert "Culinary Arts" in categories


# ---------------------------------------------------------------------------
# Stage 11 — retrieval-quality gate + refinement, wired through this
# end-to-end pipeline (unit coverage of the gate/refine logic itself is
# in tests/test_orchestrator.py; these confirm it's really connected).
# ---------------------------------------------------------------------------

def test_thin_catalog_triggers_retrieval_refinement(db_session):
    # Every catalog in this file's other tests is well under
    # orchestrator.MIN_CANDIDATES (5) on purpose, for deterministic
    # assertions on exactly which candidates come back — which makes
    # this a real, not contrived, demonstration of the "too few
    # candidates" refinement path actually firing end to end.
    engaged = _make_product(db_session, title="Engaged Course", category="Robotics Engineering")
    _make_product(db_session, title="Culinary Basics", category="Culinary Arts")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=6, now=NOW)

    assert result.strategy == "personalized"
    assert result.retrieval_refined is True


def test_rich_catalog_does_not_need_refinement(db_session):
    from app.recommendations import orchestrator as orch

    engaged = _make_product(db_session, title="Robotics Engaged", category="Robotics Engineering")
    for i in range(orch.MIN_CANDIDATES + 2):
        _make_product(db_session, title=f"Robotics Engineering Course {i}", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, top_n=6, now=NOW)

    assert result.strategy == "personalized"
    assert result.retrieval_refined is False


# ---------------------------------------------------------------------------
# HTTP layer — /dashboard
# ---------------------------------------------------------------------------

def test_dashboard_shows_personalized_recommendations(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="dashboard-demo@example.com", password="password123", display_name="Dashboard Demo"
    )
    engaged = _make_product(db_session, title="Engaged On Dashboard", category="Robotics Engineering")
    other = _make_product(db_session, title="Recommended On Dashboard", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=user.id, product_id=engaged.id, created_at=NOW)

    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Recommended for you" in response.text
    assert "Recommended On Dashboard" in response.text
    assert "Engaged On Dashboard" not in response.text
    assert "Matches your recent interest in Robotics Engineering" in response.text
    assert 'data-track-source="recommendations"' in response.text


def test_dashboard_shows_popular_fallback_for_new_user(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="cold-dashboard@example.com", password="password123", display_name="Cold Dashboard"
    )
    _make_product(db_session, title="Popular Course", category="Robotics Engineering", rating=4.9)

    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Popular Course" in response.text
    # not an exact match on POPULAR_FALLBACK_REASON: Jinja2 autoescapes the
    # apostrophe in "don't" to &#39; in the rendered HTML, so we check a
    # substring that doesn't straddle it.
    assert "Popular pick" in response.text
