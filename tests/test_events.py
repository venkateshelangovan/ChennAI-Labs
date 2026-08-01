"""
Stage 5's critical path: batched ingestion, duplicate-event handling
(explicitly called out by the challenge), product-existence validation,
session-to-user reconciliation, and that the debug view is actually
gated by admin like everything else under /admin.
"""

import uuid
from decimal import Decimal

from app.events import service as events_service
from app.events.schemas import EventIn
from app.products import service as product_service


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


def _event(**overrides):
    defaults = dict(
        client_event_id=str(uuid.uuid4()),
        session_id="session-abc",
        event_type="search",
        product_id=None,
        metadata={},
    )
    defaults.update(overrides)
    return EventIn(**defaults)


# ---------------------------------------------------------------------------
# app.events.service — unit level
# ---------------------------------------------------------------------------

def test_ingest_events_accepts_valid_batch(db_session):
    events = [_event(event_type="search", metadata={"query": "rag"})]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 1
    assert result.duplicates == 0
    assert result.rejected == 0


def test_ingest_events_rejects_unknown_event_type(db_session):
    events = [_event(event_type="teleport")]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 0
    assert result.rejected == 1


def test_ingest_events_rejects_product_required_type_without_product(db_session):
    events = [_event(event_type="view", product_id=None)]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 0
    assert result.rejected == 1


def test_ingest_events_rejects_view_for_nonexistent_product(db_session):
    events = [_event(event_type="view", product_id=999999)]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 0
    assert result.rejected == 1


def test_ingest_events_accepts_view_for_real_product(db_session):
    product = _make_product(db_session)
    events = [_event(event_type="view", product_id=product.id)]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 1


def test_ingest_events_deduplicates_within_same_batch(db_session):
    dup_id = str(uuid.uuid4())
    events = [_event(client_event_id=dup_id), _event(client_event_id=dup_id)]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 1
    assert result.duplicates == 1


def test_ingest_events_deduplicates_across_separate_calls(db_session):
    """The exact 'resend the same beacon twice' scenario sendBeacon can produce."""
    dup_id = str(uuid.uuid4())
    first = events_service.ingest_events(db_session, [_event(client_event_id=dup_id)], user_id=None)
    second = events_service.ingest_events(db_session, [_event(client_event_id=dup_id)], user_id=None)
    assert first.accepted == 1
    assert second.accepted == 0
    assert second.duplicates == 1
    assert events_service.count_events(db_session) == 1


def test_ingest_events_partial_batch_one_bad_event_does_not_block_others(db_session):
    product = _make_product(db_session)
    events = [
        _event(event_type="search", metadata={"query": "rag"}),
        _event(event_type="not-a-real-type"),
        _event(event_type="view", product_id=product.id),
    ]
    result = events_service.ingest_events(db_session, events, user_id=None)
    assert result.accepted == 2
    assert result.rejected == 1


def test_reconcile_session_attaches_anonymous_events_to_user(db_session):
    events_service.ingest_events(
        db_session, [_event(session_id="anon-session-1", event_type="search", metadata={"query": "python"})], user_id=None
    )
    updated = events_service.reconcile_session(db_session, "anon-session-1", user_id=42)
    assert updated == 1

    events = events_service.list_recent_events(db_session)
    assert events[0].user_id == 42


def test_reconcile_session_does_not_touch_other_sessions(db_session):
    events_service.ingest_events(
        db_session, [_event(session_id="session-x", event_type="search", metadata={})], user_id=None
    )
    updated = events_service.reconcile_session(db_session, "session-y", user_id=42)
    assert updated == 0


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_post_events_endpoint_accepts_anonymous_batch(client, db_session):
    product = _make_product(db_session)
    response = client.post(
        "/api/events",
        json={
            "events": [
                {
                    "client_event_id": str(uuid.uuid4()),
                    "session_id": "http-session-1",
                    "event_type": "view",
                    "product_id": product.id,
                    "metadata": {"source": "catalog"},
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1


def test_post_events_rejects_oversized_batch(client):
    events = [
        {
            "client_event_id": str(uuid.uuid4()),
            "session_id": "s",
            "event_type": "search",
            "product_id": None,
            "metadata": {},
        }
        for _ in range(51)
    ]
    response = client.post("/api/events", json={"events": events})
    assert response.status_code == 422


def test_post_events_attaches_user_id_when_logged_in(client, db_session):
    csrf = client.get("/register").cookies.get("csrf_token")
    client.post(
        "/register",
        data={
            "email": "learner@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "Learner",
            "csrf_token": csrf,
        },
    )
    client.post(
        "/api/events",
        json={
            "events": [
                {
                    "client_event_id": str(uuid.uuid4()),
                    "session_id": "http-session-2",
                    "event_type": "search",
                    "product_id": None,
                    "metadata": {"query": "dsa"},
                }
            ]
        },
    )
    events = events_service.list_recent_events(db_session)
    assert events[0].user_id is not None


def test_registering_reconciles_prior_anonymous_events(client, db_session):
    """
    The full journey: browse anonymously (events tied to a session_id
    cookie), then register — the tracker's cookie is what carries that
    session_id into the registration request for reconciliation.
    """
    client.cookies.set("cl_session_id", "pre-reg-session")
    client.post(
        "/api/events",
        json={
            "events": [
                {
                    "client_event_id": str(uuid.uuid4()),
                    "session_id": "pre-reg-session",
                    "event_type": "search",
                    "product_id": None,
                    "metadata": {"query": "agentic ai"},
                }
            ]
        },
    )
    events_before = events_service.list_recent_events(db_session)
    assert events_before[0].user_id is None

    csrf = client.get("/register").cookies.get("csrf_token")
    client.post(
        "/register",
        data={
            "email": "newlearner@example.com",
            "password": "password123",
            "confirm_password": "password123",
            "display_name": "New Learner",
            "csrf_token": csrf,
        },
    )

    # The register request committed through a *different* Session
    # instance (the one FastAPI's dependency override handed to that
    # request) than db_session here. db_session already cached the event
    # row in its identity map from the query above, so without expiring
    # it, SQLAlchemy would hand back the stale in-memory object instead
    # of re-reading the now-updated row — a test-harness quirk, not
    # something the app itself needs to care about.
    db_session.expire_all()
    events_after = events_service.list_recent_events(db_session)
    assert events_after[0].user_id is not None


def test_admin_events_view_requires_admin(client):
    response = client.get("/admin/events", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_events_view_renders_for_admin(client, db_session):
    from app.auth import service as auth_service

    admin = auth_service.register_user(
        db_session, email="admin@example.com", password="password123", display_name="Admin", role="admin"
    )
    session = auth_service.create_session(db_session, admin)
    client.cookies.set("session_token", session.token)

    events_service.ingest_events(
        db_session, [_event(session_id="visible-session", event_type="search", metadata={"query": "nlp"})], user_id=None
    )

    response = client.get("/admin/events")
    assert response.status_code == 200
    assert "nlp" in response.text or "search" in response.text
