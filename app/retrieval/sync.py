"""
The dual-write orchestration described in Stage 0, Section 9. This is
the only module that decides *when* a product gets (re-)embedded and
what happens if the vector write fails — app/products/service.py calls
`sync_product`/`remove_product` after its own SQL commit succeeds, and
never touches chromadb or the embedding provider directly.

The core rule, restated from the architecture doc: SQL is always the
write that must succeed. The vector write is attempted after, and if it
fails, we do not roll back the SQL write, we do not raise, and we do
not silently pretend it worked — we flag it (`vector_sync_status =
'failed'`) and make it repairable (`reconcile()` below, wired to an
admin action in app/admin/routes.py).
"""

import logging

from sqlalchemy.orm import Session

from app.db.models.product import Product
from app.retrieval import vector_store
from app.retrieval.embeddings import build_embedding_text, compute_content_hash, get_embedding_provider

logger = logging.getLogger("chennai_labs.retrieval")


def _metadata_for(product: Product) -> dict:
    return {
        "category": product.category,
        "subcategory": product.subcategory or "",
        "level": product.level,
        "status": product.status,
        "price": float(product.price),
    }


def sync_product(db: Session, product: Product) -> None:
    """
    (Re-)embed and upsert a single product if — and only if — its
    embed-relevant content actually changed since the last successful
    sync, or the last attempt failed. This is the concrete answer to
    "what does NOT cause an AI call": editing price/instructor/image
    alone, or calling this twice in a row on an already-synced product,
    does nothing.
    """
    text = build_embedding_text(product)
    new_hash = compute_content_hash(text)

    if product.vector_sync_status == "synced" and product.content_hash == new_hash:
        return

    try:
        embedding = get_embedding_provider().embed(text)
        vector_store.upsert_product(
            product_id=product.id, embedding=embedding, document=text, metadata=_metadata_for(product)
        )
        product.content_hash = new_hash
        product.vector_sync_status = "synced"
    except Exception:  # noqa: BLE001 — any vector-store failure is handled the same way
        logger.exception("vector_sync_failed", extra={"product_id": product.id})
        # Deliberately do NOT update content_hash here — leaving it
        # stale/None guarantees the next sync_product() call sees a
        # hash mismatch and retries, with no separate "force" flag needed.
        product.vector_sync_status = "failed"

    db.commit()


def remove_product(db: Session, product: Product) -> None:
    """
    Used on archive. Deletes outright from the index rather than
    relying on a metadata filter (Stage 0, Section 8) — defense in
    depth, so a bug in a `where={"status": "active"}` filter elsewhere
    can never surface an archived product. Resets sync state so that if
    the product is later restored, sync_product() re-embeds it
    unconditionally rather than trusting a stale hash.
    """
    vector_store.delete_product(product.id)
    product.content_hash = None
    product.vector_sync_status = "pending"
    db.commit()


def detect_drift(db: Session) -> dict:
    """
    Compares SQL's view of "what should be searchable" against what's
    actually in the vector index — catches inconsistencies beyond the
    ones `vector_sync_status` already flags (e.g. manual data fixes,
    or a vector write that reported success but didn't actually land).
    """
    sql_active_ids = {row[0] for row in db.query(Product.id).filter(Product.status == "active").all()}
    vector_ids = vector_store.get_synced_ids()
    return {
        "missing_from_vector": sorted(sql_active_ids - vector_ids),
        "orphaned_in_vector": sorted(vector_ids - sql_active_ids),
    }


def reconcile(db: Session) -> dict:
    """
    The repair sweep: retry every active product that isn't marked
    'synced', and clean up any orphaned vector entries drift detection
    finds (archived/deleted products whose vector somehow wasn't
    removed). Wired to an admin "Sync now" action — not run
    automatically on a schedule yet (that's Stage 15 territory, if
    justified).
    """
    candidates = (
        db.query(Product)
        .filter(Product.status == "active", Product.vector_sync_status != "synced")
        .all()
    )
    succeeded, failed = 0, 0
    for product in candidates:
        sync_product(db, product)
        if product.vector_sync_status == "synced":
            succeeded += 1
        else:
            failed += 1

    drift = detect_drift(db)
    for orphan_id in drift["orphaned_in_vector"]:
        vector_store.delete_product(orphan_id)

    return {
        "retried": len(candidates),
        "succeeded": succeeded,
        "failed": failed,
        "orphans_removed": len(drift["orphaned_in_vector"]),
    }
