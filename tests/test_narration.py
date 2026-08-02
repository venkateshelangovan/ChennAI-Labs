"""
Stage 10's critical path: grounding validation. The LLM never decides
WHAT to recommend (that's Stage 8, untouched here) — it only writes a
sentence or two about an already-final list, and every one of these
tests exists to prove a hallucinated or unreachable-Mesh response can
never reach a user; it always falls back to no narration instead.

No real Mesh calls: every test monkeypatches app.mesh.client.chat
directly. tests/conftest.py's autouse no_real_mesh_chat_calls fixture
already defaults `chat` to raising MeshAPIError for every other test in
the suite — these tests override it per-test to exercise the success
and grounding-failure paths deliberately.
"""

from decimal import Decimal

import pytest

from app.mesh import client as mesh_client
from app.products import service as product_service
from app.recommendations import narration
from app.recommendations.schemas import Recommendation


def _make_product(db, **overrides):
    defaults = dict(
        title="Retrieval-Augmented Generation (RAG) in Production",
        description="Chunking, embeddings, vector search, and grounding.",
        category="Generative AI",
        subcategory="RAG",
        price=Decimal("6999"),
        level="advanced",
        tags=["rag"],
        instructor="Rahul Chatterjee",
        duration_minutes=1800,
        image_url=None,
    )
    defaults.update(overrides)
    return product_service.create_product(db, **defaults)


def _rec(product, reason="because reasons", similarity=0.8):
    return Recommendation(product=product, reason=reason, similarity=similarity)


def test_no_recommendations_falls_back_without_calling_mesh(monkeypatch):
    def _should_not_be_called(*a, **k):
        raise AssertionError("chat() should never be called with an empty recommendation list")

    monkeypatch.setattr(mesh_client, "chat", _should_not_be_called)

    result = narration.generate_narration([])

    assert result.text is None
    assert result.grounded is False
    assert result.fallback_reason == "no_recommendations"


def test_grounded_response_substitutes_citations_with_real_titles(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A", category="Cat A")
    b = _make_product(db_session, title="Course B", category="Cat B")
    recs = [_rec(a), _rec(b)]

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "You'd love [1] and [2] together!")

    result = narration.generate_narration(recs)

    assert result.grounded is True
    assert result.fallback_reason is None
    assert "Course A" in result.text
    assert "Course B" in result.text
    assert "[1]" not in result.text and "[2]" not in result.text


def test_response_with_out_of_range_citation_falls_back(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    recs = [_rec(a)]  # only [1] is valid

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "Check out [1] and also [5]!")

    result = narration.generate_narration(recs)

    assert result.grounded is False
    assert result.text is None
    assert result.fallback_reason == "ungrounded_citation"


def test_response_with_zero_citation_falls_back(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    recs = [_rec(a)]

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "Check out course [0]!")

    result = narration.generate_narration(recs)

    assert result.grounded is False
    assert result.fallback_reason == "ungrounded_citation"


def test_response_with_no_citations_is_still_grounded(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    recs = [_rec(a)]

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "Here's a great pick for you this week.")

    result = narration.generate_narration(recs)

    assert result.grounded is True
    assert result.text == "Here's a great pick for you this week."


def test_mesh_error_falls_back(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    recs = [_rec(a)]

    def _raise(*a, **k):
        raise mesh_client.MeshAPIError("simulated outage")

    monkeypatch.setattr(mesh_client, "chat", _raise)

    result = narration.generate_narration(recs)

    assert result.text is None
    assert result.grounded is False
    assert result.fallback_reason == "mesh_error"


def test_prompt_includes_every_recommendation_title_and_category(db_session):
    a = _make_product(db_session, title="Course A", category="Cat A")
    b = _make_product(db_session, title="Course B", category="Cat B")
    recs = [_rec(a), _rec(b)]

    messages = narration._build_messages(recs)
    user_content = messages[1]["content"]

    assert "[1] Course A" in user_content
    assert "Cat A" in user_content
    assert "[2] Course B" in user_content
    assert "Cat B" in user_content


def test_grounding_rejects_when_any_citation_invalid_even_if_others_valid(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    b = _make_product(db_session, title="Course B")
    recs = [_rec(a), _rec(b)]

    assert narration._validate_grounding("great picks: [1] and [2]", recs) is True
    assert narration._validate_grounding("great picks: [1] and [3]", recs) is False


# ---------------------------------------------------------------------------
# HTTP layer — /dashboard's narration callout
# ---------------------------------------------------------------------------

def test_dashboard_shows_grounded_narration(client, db_session, monkeypatch):
    from app.auth import service as auth_service

    user = auth_service.register_user(
        db_session, email="narration-demo@example.com", password="password123", display_name="Narration Demo"
    )
    _make_product(db_session, title="Only Course Available", category="Solo Category", rating=4.9)
    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    # Exactly one product exists -> exactly one recommendation -> [1] is
    # guaranteed valid regardless of ranking details.
    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "You should check out [1] — it's a great start!")

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "AI summary" in response.text
    assert "Only Course Available" in response.text


def test_dashboard_renders_normally_when_mesh_chat_unavailable(client, db_session):
    from app.auth import service as auth_service

    # No monkeypatch here — relies on conftest.py's autouse
    # no_real_mesh_chat_calls fixture, which is exactly the default
    # every other dashboard test in the suite already depends on.
    user = auth_service.register_user(
        db_session, email="no-narration-demo@example.com", password="password123", display_name="No Narration"
    )
    _make_product(db_session, title="Some Course", rating=4.5)
    session = auth_service.create_session(db_session, user)
    client.cookies.set("session_token", session.token)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "AI summary" not in response.text
    assert "Some Course" in response.text
