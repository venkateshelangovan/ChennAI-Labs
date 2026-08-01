"""
Stage 4's critical path: the dual-write mechanism. Per the challenge's
explicit requirement, this specifically tests create/update/delete and
— most importantly — failure scenarios: a vector-store failure must
never fail the SQL write, must be flagged, and must be repairable.
"""

from decimal import Decimal

import pytest

from app.products import service as product_service
from app.retrieval import sync as vector_sync
from app.retrieval import vector_store
from app.retrieval.embeddings import LocalHashEmbeddingProvider, build_embedding_text, compute_content_hash


def _make_product(db, **overrides):
    defaults = dict(
        title="Retrieval-Augmented Generation (RAG) in Production",
        description="Chunking, embeddings, vector search, and grounding.",
        category="Generative AI",
        subcategory="RAG",
        price=Decimal("6999"),
        level="advanced",
        tags=["rag", "embeddings"],
        instructor="Rahul Chatterjee",
        duration_minutes=1800,
        image_url=None,
    )
    defaults.update(overrides)
    return product_service.create_product(db, **defaults)


# ---------------------------------------------------------------------------
# Embedding text + local placeholder provider
# ---------------------------------------------------------------------------

def test_build_embedding_text_includes_key_fields(db_session):
    product = _make_product(db_session)
    text = build_embedding_text(product)
    assert "Title: Retrieval-Augmented Generation" in text
    assert "Category: Generative AI > RAG" in text
    assert "Level: advanced" in text
    assert "rag" in text


def test_local_embedding_provider_is_deterministic():
    provider = LocalHashEmbeddingProvider()
    v1 = provider.embed("agentic AI and RAG")
    v2 = provider.embed("agentic AI and RAG")
    assert v1 == v2


def test_local_embedding_provider_differs_for_different_text():
    provider = LocalHashEmbeddingProvider()
    v1 = provider.embed("agentic AI and RAG")
    v2 = provider.embed("data structures and algorithms")
    assert v1 != v2


def test_compute_content_hash_changes_with_text():
    assert compute_content_hash("a") != compute_content_hash("b")
    assert compute_content_hash("a") == compute_content_hash("a")


# ---------------------------------------------------------------------------
# Vector store wrapper, directly
# ---------------------------------------------------------------------------

def test_vector_store_upsert_query_delete():
    provider = LocalHashEmbeddingProvider()
    vector_store.upsert_product(
        product_id=1, embedding=provider.embed("rag course"), document="rag course", metadata={"status": "active"}
    )
    results = vector_store.query(embedding=provider.embed("rag course"), top_k=5)
    assert len(results) == 1
    assert results[0]["id"] == 1

    vector_store.delete_product(1)
    assert vector_store.query(embedding=provider.embed("rag course"), top_k=5) == []


# ---------------------------------------------------------------------------
# Dual-write: create / update / archive / restore
# ---------------------------------------------------------------------------

def test_create_product_syncs_to_vector_store(db_session):
    product = _make_product(db_session)
    assert product.vector_sync_status == "synced"
    assert product.content_hash is not None
    assert vector_store.count() == 1


def test_update_with_unrelated_field_change_skips_reembedding(db_session, monkeypatch):
    product = _make_product(db_session)
    original_hash = product.content_hash

    embed_calls = []
    original_embed = LocalHashEmbeddingProvider.embed

    def counting_embed(self, text):
        embed_calls.append(text)
        return original_embed(self, text)

    monkeypatch.setattr(LocalHashEmbeddingProvider, "embed", counting_embed)

    # Price is not part of the embedded text — updating only it must not re-embed.
    product_service.update_product(
        db_session,
        product,
        title=product.title,
        description=product.description,
        category=product.category,
        subcategory=product.subcategory,
        price=Decimal("7999"),
        level=product.level,
        tags=product.tags,
        instructor=product.instructor,
        duration_minutes=product.duration_minutes,
        image_url=product.image_url,
    )
    assert embed_calls == []
    assert product.content_hash == original_hash


def test_update_with_title_change_triggers_reembedding(db_session):
    product = _make_product(db_session)
    original_hash = product.content_hash

    product_service.update_product(
        db_session,
        product,
        title="RAG in Production (Advanced Edition)",
        description=product.description,
        category=product.category,
        subcategory=product.subcategory,
        price=product.price,
        level=product.level,
        tags=product.tags,
        instructor=product.instructor,
        duration_minutes=product.duration_minutes,
        image_url=product.image_url,
    )
    assert product.content_hash != original_hash
    assert product.vector_sync_status == "synced"


def test_archive_removes_from_vector_store(db_session):
    product = _make_product(db_session)
    assert vector_store.count() == 1

    product_service.archive_product(db_session, product)
    assert vector_store.count() == 0
    assert product.vector_sync_status == "pending"
    assert product.content_hash is None


def test_restore_resyncs_to_vector_store(db_session):
    product = _make_product(db_session)
    product_service.archive_product(db_session, product)
    assert vector_store.count() == 0

    product_service.restore_product(db_session, product)
    assert vector_store.count() == 1
    assert product.vector_sync_status == "synced"


# ---------------------------------------------------------------------------
# Failure handling — the challenge's explicit "test synchronization failure"
# ---------------------------------------------------------------------------

def test_vector_store_failure_does_not_fail_sql_write(db_session, monkeypatch):
    def broken_upsert(**kwargs):
        raise RuntimeError("simulated vector store outage")

    monkeypatch.setattr(vector_store, "upsert_product", broken_upsert)

    # create_product must still succeed and return a real, persisted row —
    # the SQL write is never rolled back because the vector write failed.
    product = _make_product(db_session, title="Product During Outage")
    assert product.id is not None
    assert product_service.get_product_by_id(db_session, product.id).title == "Product During Outage"
    assert product.vector_sync_status == "failed"
    assert product.content_hash is None  # never updated on failure


def test_reconcile_retries_failed_products_after_outage_ends(db_session, monkeypatch):
    def broken_upsert(**kwargs):
        raise RuntimeError("simulated vector store outage")

    monkeypatch.setattr(vector_store, "upsert_product", broken_upsert)
    product = _make_product(db_session, title="Product During Outage 2")
    assert product.vector_sync_status == "failed"

    monkeypatch.undo()  # "outage" ends — restore the real upsert_product

    result = vector_sync.reconcile(db_session)
    assert result["succeeded"] == 1
    assert result["failed"] == 0
    db_session.refresh(product)
    assert product.vector_sync_status == "synced"


def test_detect_drift_finds_orphaned_vector_entries(db_session):
    product = _make_product(db_session)
    # Simulate drift: something removed the SQL row's "active-ness" out of
    # band without going through archive_product (which would have cleaned
    # up the vector entry itself).
    product.status = "archived"
    db_session.commit()

    drift = vector_sync.detect_drift(db_session)
    assert product.id in drift["orphaned_in_vector"]
    assert drift["missing_from_vector"] == []


def test_reconcile_removes_orphaned_vector_entries(db_session):
    product = _make_product(db_session)
    product.status = "archived"  # drift, as above — bypassing archive_product on purpose
    db_session.commit()
    assert vector_store.count() == 1

    result = vector_sync.reconcile(db_session)
    assert result["orphans_removed"] == 1
    assert vector_store.count() == 0


def test_sync_product_is_noop_when_already_synced_and_unchanged(db_session, monkeypatch):
    product = _make_product(db_session)

    embed_calls = []
    original_embed = LocalHashEmbeddingProvider.embed

    def counting_embed(self, text):
        embed_calls.append(text)
        return original_embed(self, text)

    monkeypatch.setattr(LocalHashEmbeddingProvider, "embed", counting_embed)

    vector_sync.sync_product(db_session, product)  # nothing changed since creation
    assert embed_calls == []
