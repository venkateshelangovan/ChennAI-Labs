"""
Stage 14: the admin "behavior & recommendations" view (Journey 3) and
the trace data it renders. Two layers, same split as the rest of this
stage's tests:

1. Trace CAPTURE — does app/recommendations/service.py's
   generate_recommendations and app/recommendations/narration.py's
   generate_narration actually populate the fields the admin view
   depends on, for every code path (cold start, no candidates, all
   candidates already engaged, personalized, and the narration
   success/hallucination/error paths)?
2. Trace DISPLAY — does the admin route + template actually surface
   what's on the RecommendationSnapshot row, and is it properly gated
   by require_admin like every other /admin page?
"""

import uuid
from decimal import Decimal

from app.core.time import utcnow
from app.db.models.user_event import UserEvent
from app.mesh import client as mesh_client
from app.products import service as product_service
from app.recommendations import cache as reco_cache
from app.recommendations import service as reco_service
from app.recommendations.narration import generate_narration

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
# Trace capture — generate_recommendations
# ---------------------------------------------------------------------------

def test_cold_start_trace_records_the_path(db_session):
    _make_product(db_session, title="Some Course")
    result = reco_service.generate_recommendations(db_session, user_id=1, now=NOW)
    assert result.trace["path"] == "cold_start"


def test_personalized_trace_includes_retrieval_attempts_and_candidate_pool(db_session):
    engaged = _make_product(db_session, title="Engaged", category="Robotics Engineering")
    other = _make_product(db_session, title="Other", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=1, product_id=engaged.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, now=NOW)

    assert result.trace["path"] == "personalized"
    assert "retrieval_attempts" in result.trace
    assert len(result.trace["retrieval_attempts"]) >= 1
    candidate_ids = [c["product_id"] for c in result.trace["candidate_pool"]]
    assert other.id in candidate_ids
    assert result.trace["engaged_product_ids"] == [engaged.id]


def test_all_candidates_engaged_trace_records_the_path(db_session):
    only = _make_product(db_session, title="Only Course")
    _insert_view_event(db_session, user_id=1, product_id=only.id, created_at=NOW)

    result = reco_service.generate_recommendations(db_session, user_id=1, now=NOW)

    assert result.trace["path"] == "all_candidates_already_engaged"
    assert result.trace["engaged_product_ids"] == [only.id]


# ---------------------------------------------------------------------------
# Trace capture — generate_narration
# ---------------------------------------------------------------------------

def test_grounded_narration_records_raw_text(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    from app.recommendations.schemas import Recommendation

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "You'd love [1]!")
    result = generate_narration([Recommendation(product=a, reason="x", similarity=0.9)])

    assert result.grounded is True
    assert result.raw_text == "You'd love [1]!"
    assert result.rejected_citations == []


def test_hallucinated_narration_records_rejected_citations(db_session, monkeypatch):
    a = _make_product(db_session, title="Course A")
    from app.recommendations.schemas import Recommendation

    monkeypatch.setattr(mesh_client, "chat", lambda *a, **k: "Check out [1] and also [7]!")
    result = generate_narration([Recommendation(product=a, reason="x", similarity=0.9)])

    assert result.grounded is False
    assert result.rejected_citations == [7]
    assert result.raw_text == "Check out [1] and also [7]!"


# ---------------------------------------------------------------------------
# Admin view — access control
# ---------------------------------------------------------------------------

def test_admin_recommendations_list_requires_admin(client):
    response = client.get("/admin/recommendations", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_recommendations_detail_requires_admin(client, db_session):
    target = _make_product(db_session)  # irrelevant, just needs a DB touch
    response = client.get("/admin/recommendations/1", follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------------
# Admin view — rendering
# ---------------------------------------------------------------------------

def test_admin_recommendations_list_shows_generated_snapshot(client, db_session):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="learner@example.com", password="password123", display_name="Learner"
    )
    _make_product(db_session, title="Some Course")
    reco_cache.get_dashboard_recommendations(db_session, learner.id, now=NOW)

    _login(client, db_session, admin)
    response = client.get("/admin/recommendations")

    assert response.status_code == 200
    assert "Learner" in response.text
    assert "no_snapshot" in response.text  # trigger_reason for a first-ever generation


def test_admin_recommendations_detail_shows_full_trace(client, db_session):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="learner2@example.com", password="password123", display_name="Learner Two"
    )
    engaged = _make_product(db_session, title="Engaged Course", category="Robotics Engineering")
    other = _make_product(db_session, title="Other Course", category="Robotics Engineering")
    _insert_view_event(db_session, user_id=learner.id, product_id=engaged.id, created_at=NOW)

    reco_cache.get_dashboard_recommendations(db_session, learner.id, now=NOW)

    _login(client, db_session, admin)
    response = client.get(f"/admin/recommendations/{learner.id}")

    assert response.status_code == 200
    assert "Other Course" in response.text
    assert "personalized" in response.text
    assert "Engaged Course" not in response.text or "already engaged" in response.text.lower()


def test_admin_recommendations_detail_handles_user_with_no_snapshot(client, db_session):
    admin = _register_admin(db_session)
    from app.auth import service as auth_service

    learner = auth_service.register_user(
        db_session, email="no-snapshot@example.com", password="password123", display_name="No Snapshot"
    )
    _login(client, db_session, admin)

    response = client.get(f"/admin/recommendations/{learner.id}")

    assert response.status_code == 200
    assert "No recommendation generated yet" in response.text


def test_admin_recommendations_detail_unknown_user_redirects(client, db_session):
    admin = _register_admin(db_session)
    _login(client, db_session, admin)

    response = client.get("/admin/recommendations/99999", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/recommendations"
