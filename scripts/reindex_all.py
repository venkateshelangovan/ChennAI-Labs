"""
Stage 9's one-time operational migration: force every active product
to be re-embedded from scratch under whatever get_embedding_provider()
currently returns.

Why this is needed and can't be automatic: app/retrieval/sync.py's
reconcile() (the existing "Sync now" admin action) only retries
products whose vector_sync_status ISN'T 'synced' — by design, so a
routine sync doesn't silently re-embed the whole catalog on every call.
A provider swap (the local hash placeholder -> Mesh) needs something
stronger: every already-'synced' row is now stale — embedded in a
vector space nothing will be compared against again — and its old
vector sits at the OLD provider's dimension, which would make Chroma
reject a fresh upsert at the NEW provider's dimension outright rather
than quietly coexist. `compute_content_hash` folds in
EMBEDDING_SCHEMA_VERSION precisely so a provider swap invalidates the
hash going forward, but that alone doesn't retroactively touch rows
already marked 'synced' under the old version — this script is the
explicit step that does.

Deliberately explicit and manual, not triggered automatically on
provider change: re-embedding an entire catalog is a real, potentially
billed, potentially slow operation against an external API. That
should always be something a human decides to run, never a side effect
of a code deploy.

What it does:
1. Resets the vector store outright (deletes and recreates the Chroma
   collection) — the only way to guarantee no dimension mismatch.
2. Clears every active product's content_hash and marks it 'pending' —
   the same state a brand-new product starts in.
3. Runs the existing reconcile() sweep to re-embed everything through
   the current embedding provider.

Usage:
    python -m scripts.reindex_all
"""

import logging

from app.core.logging import configure_logging
from app.db.models.product import Product
from app.db.session import SessionLocal
from app.retrieval import sync as vector_sync
from app.retrieval import vector_store

configure_logging()
logger = logging.getLogger("chennai_labs.scripts.reindex_all")


def main() -> None:
    db = SessionLocal()
    try:
        vector_store.reset_collection()

        marked = (
            db.query(Product)
            .filter(Product.status == "active")
            .update({Product.content_hash: None, Product.vector_sync_status: "pending"}, synchronize_session=False)
        )
        db.commit()
        logger.info("reindex_reset", extra={"products_marked_pending": marked})

        result = vector_sync.reconcile(db)
        logger.info("reindex_complete", extra=result)
        print(
            f"Reindexed {result['succeeded']} product(s); {result['failed']} failed "
            f"(check vector_sync_status for retry); {result['orphans_removed']} orphaned "
            f"vector entries removed."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
