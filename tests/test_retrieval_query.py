"""
Stage 7's critical path: turning an interest profile into a query
against the vector store, and getting sensible candidates back — using
today's non-semantic placeholder embeddings (see app/retrieval/query.py's
module docstring), so "sensible" here means "shares vocabulary with the
right product," not "understands meaning."

Products created via product_service.create_product are auto-synced to
the (test-isolated, per conftest.py's autouse isolated_vector_store
fixture) vector store as a side effect — no separate sync step needed.
"""

import uuid
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.user_event import UserEvent
from app.products import service as product_service
from app.retrieval import query as retrieval_query
from app.retrieval import vector_store
from app.retrieval.embeddings import build_embedding_text, get_embedding_provider

NOW = utcnow()


def _make_product(db, **overrides):
    defaults = dict(
        title="Data Structures & Algorithms for MAANG Interviews",
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


def _insert_view_event(db, *, user_id, product_id, created_at=None, session_id="s"):
    event = UserEvent(
        user_id=user_id,
        session_id=session_id,
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
# _profile_to_query_text — pure function, no DB needed
# ---------------------------------------------------------------------------

def test_query_text_repeats_category_tokens_proportional_to_score():
    from app.profile.schemas import CategoryScore, InterestProfile

    profile = InterestProfile(
        user_id=1,
        generated_at=NOW,
        sample_size=1,
        category_scores=[CategoryScore(category="RAG", score=1.0, evidence_count=1)],
    )
    text = retrieval_query._profile_to_query_text(profile)
    assert text.split().count("RAG") == retrieval_query.CATEGORY_REPEAT_SCALE


def test_query_text_includes_search_terms_and_top_products():
    from app.profile.schemas import InterestProfile, ProductAffinity, SearchTerm

    profile = InterestProfile(
        user_id=1,
        generated_at=NOW,
        sample_size=2,
        top_products=[
            ProductAffinity(
                product_id=1, slug="x", title="Reinforcement Learning Deep Dive", category="RL", relative_score=1.0, evidence_count=1
            )
        ],
        search_terms=[SearchTerm(query="agentic workflows", count=1, last_searched_at=NOW)],
    )
    text = retrieval_query._profile_to_query_text(profile)
    assert "Reinforcement Learning Deep Dive" in text
    assert "agentic workflows" in text


# ---------------------------------------------------------------------------
# retrieve_for_user — end to end against the (isolated) real vector store
# ---------------------------------------------------------------------------

def test_cold_start_profile_returns_no_candidates(db_session):
    result = retrieval_query.retrieve_for_user(db_session, user_id=1, now=NOW)
    assert result.is_cold_start
    assert result.candidates == []
    assert result.query_text == ""


def test_retrieval_ranks_matching_category_product_first(db_session):
    matching = _make_product(db_session, category="DSA / Interview Prep", title="DSA Course", tags=["dsa"])
    unrelated = _make_product(
        db_session,
        category="Deep Learning Foundations",
        title="Neural Networks From Scratch",
        tags=["deep-learning"],
    )
    _insert_view_event(db_session, user_id=1, product_id=matching.id, created_at=NOW)

    result = retrieval_query.retrieve_for_user(db_session, user_id=1, now=NOW, top_k=5)

    assert not result.is_cold_start
    assert len(result.candidates) == 2
    assert result.candidates[0].product_id == matching.id
    assert result.candidates[0].similarity > result.candidates[1].similarity
    assert unrelated.id in [c.product_id for c in result.candidates]


def test_retrieval_excludes_archived_products_via_status_filter(db_session):
    """
    Defense in depth: archive_product() already deletes from the vector
    index (app/retrieval/sync.py), so this simulates a product that
    somehow stayed indexed as 'archived' (e.g. a bug in that path) to
    prove the where={"status": "active"} filter is a real second line
    of defense, not dead code.
    """
    product = _make_product(db_session, category="Ghost Category")
    _insert_view_event(db_session, user_id=1, product_id=product.id, created_at=NOW)

    # Simulate drift: force the indexed metadata to say "archived"
    # without actually removing it, bypassing the normal archive path.
    text = build_embedding_text(product)
    vector_store.upsert_product(
        product_id=product.id,
        embedding=get_embedding_provider().embed(text),
        document=text,
        metadata={"category": product.category, "subcategory": "", "level": product.level, "status": "archived", "price": 1.0},
    )

    result = retrieval_query.retrieve_for_user(db_session, user_id=1, now=NOW)

    assert result.candidates == []


def test_retrieval_respects_top_k(db_session):
    for i in range(retrieval_query.DEFAULT_TOP_K + 3):
        product = _make_product(db_session, title=f"Course {i}", category="Shared Category")
        if i == 0:
            _insert_view_event(db_session, user_id=1, product_id=product.id, created_at=NOW)

    result = retrieval_query.retrieve_for_user(db_session, user_id=1, now=NOW, top_k=4)

    assert len(result.candidates) == 4


def test_retrieval_returns_empty_candidates_when_vector_store_is_empty(db_session):
    """A profile can have real signal (a category_view needs no product
    row) while the vector store has nothing indexed at all — should not
    crash, just return no candidates."""
    event = UserEvent(
        user_id=1,
        session_id="s",
        event_type="category_view",
        product_id=None,
        event_metadata={"category": "Untracked Category"},
        client_event_id=str(uuid.uuid4()),
        created_at=NOW,
    )
    db_session.add(event)
    db_session.commit()

    result = retrieval_query.retrieve_for_user(db_session, user_id=1, now=NOW)

    assert not result.is_cold_start
    assert result.query_text != ""
    assert result.candidates == []


# ---------------------------------------------------------------------------
# HTTP layer — the /profile retrieval preview
# ---------------------------------------------------------------------------

def test_profile_page_shows_retrieval_preview_with_candidates(client, db_session):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="retrieval-demo@example.com", password="password123", display_name="Retrieval Demo"
    )
    product = _make_product(db_session, category="Agentic AI", title="Agentic AI Systems in Production")
    _insert_view_event(db_session, user_id=user.id, product_id=product.id, created_at=NOW)

    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    response = client.get("/profile")

    assert response.status_code == 200
    assert "Retrieval preview" in response.text
    assert "Agentic AI Systems in Production" in response.text
    # Stage 18: this copy used to claim retrieval "doesn't have ranking
    # logic, novelty, or business rules on top of it yet (that's Stage
    # 8)" and that embeddings were "still a local, non-semantic
    # placeholder (Stage 9 swaps in Mesh)" — both true when written at
    # Stage 7, both stale and actively misleading by the time Stage 8/9
    # actually shipped. Fixed to describe what's actually still true
    # today: this preview is upstream of novelty/diversity/caching, not
    # a claim about which embedding provider is active.
    assert "not the same list as your dashboard" in response.text
