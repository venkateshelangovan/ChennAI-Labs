"""
A thin wrapper around Chroma — `upsert`, `delete`, `query`, nothing
else. Every other module in the codebase (app/retrieval/sync.py today;
the recommendation retrieval pipeline from Stage 7 onward) talks to
this interface, never to the `chromadb` package directly. That's what
makes "swap Chroma for Qdrant later" (Stage 0, Section 8) a change
contained to this one file.

Chroma runs embedded — no separate service, persisted to
`settings.vector_db_path` on disk. We pass embeddings in explicitly on
every call (computed via app/retrieval/embeddings.py) rather than
configuring Chroma with its own embedding function, because the
embedding step is Mesh's job (Stage 9), not the vector store's.
"""

import chromadb

from app.core.config import settings

COLLECTION_NAME = "products"

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=settings.vector_db_path)
    return _client


def _get_collection():
    return _get_client().get_or_create_collection(name=COLLECTION_NAME)


def upsert_product(*, product_id: int, embedding: list[float], document: str, metadata: dict) -> None:
    """
    Upsert is idempotent by design (Stage 0, Section 9) — the vector ID
    is always the product's SQL ID, so retrying a failed sync is always
    safe: it can never create a duplicate or drift into an inconsistent
    second entry for the same product.
    """
    _get_collection().upsert(
        ids=[str(product_id)],
        embeddings=[embedding],
        documents=[document],
        metadatas=[metadata],
    )


def delete_product(product_id: int) -> None:
    """
    Safe to call even if the ID was never synced — Chroma's delete is a
    no-op on a missing ID rather than an error, which matters for the
    archive path (a product that failed its initial sync and is then
    archived should still "delete cleanly").
    """
    _get_collection().delete(ids=[str(product_id)])


def query(*, embedding: list[float], top_k: int = 10, where: dict | None = None) -> list[dict]:
    """
    Returns a list of {"id": int, "distance": float, "metadata": dict}
    ordered nearest-first. `where` is a Chroma metadata filter, e.g.
    {"status": "active"} — used starting Stage 7 to restrict candidates
    without over-fetching and filtering in Python.
    """
    collection = _get_collection()
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[embedding],
        n_results=min(top_k, collection.count()),
        where=where,
    )
    ids = result["ids"][0]
    distances = result["distances"][0]
    metadatas = result["metadatas"][0]
    return [
        {"id": int(id_), "distance": dist, "metadata": meta}
        for id_, dist, meta in zip(ids, distances, metadatas)
    ]


def count() -> int:
    return _get_collection().count()


def get_synced_ids() -> set[int]:
    """Every product ID currently present in the vector index — used by
    the reconciliation sweep to detect drift beyond flagged failures."""
    collection = _get_collection()
    if collection.count() == 0:
        return set()
    result = collection.get(include=[])
    return {int(id_) for id_ in result["ids"]}


def health_check() -> None:
    """Raises if the vector store isn't reachable. Used by /health."""
    _get_collection().count()
