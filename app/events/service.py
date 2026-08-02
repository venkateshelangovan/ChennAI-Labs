"""
Event ingestion and the session-to-user reconciliation that makes
pre-registration behavior count. Everything here is deterministic
bookkeeping — no AI, no judgment calls about what a user "means,"
just validating and storing what actually happened. Interpreting these
events into an interest profile is Stage 6's job.
"""

import logging

from sqlalchemy.orm import Session

from app.db.models.product import Product
from app.db.models.user_event import VALID_EVENT_TYPES, UserEvent
from app.events.schemas import EventIn

logger = logging.getLogger("chennai_labs.events")

# Event types that are meaningless without a real product behind them —
# a "view" or "click" or "time_spent" with no valid product is dropped
# rather than stored as a row that would only confuse later aggregation.
PRODUCT_REQUIRED_TYPES = {"view", "click", "time_spent"}


class IngestResult:
    def __init__(self, accepted: int, duplicates: int, rejected: int):
        self.accepted = accepted
        self.duplicates = duplicates
        self.rejected = rejected

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "duplicates": self.duplicates, "rejected": self.rejected}


def ingest_events(db: Session, events: list[EventIn], *, user_id: int | None) -> IngestResult:
    if not events:
        return IngestResult(0, 0, 0)

    # --- dedup: which client_event_ids in this batch have we already stored? ---
    incoming_ids = [e.client_event_id for e in events]
    existing_ids = {
        row[0]
        for row in db.query(UserEvent.client_event_id)
        .filter(UserEvent.client_event_id.in_(incoming_ids))
        .all()
    }

    # --- product existence: one query for every product_id referenced, not one-per-event ---
    referenced_product_ids = {e.product_id for e in events if e.product_id is not None}
    existing_product_ids = set()
    if referenced_product_ids:
        existing_product_ids = {
            row[0] for row in db.query(Product.id).filter(Product.id.in_(referenced_product_ids)).all()
        }

    duplicates = 0
    rejected = 0
    rows: list[UserEvent] = []
    seen_in_batch: set[str] = set()

    for event in events:
        if event.client_event_id in existing_ids or event.client_event_id in seen_in_batch:
            duplicates += 1
            continue
        seen_in_batch.add(event.client_event_id)

        if event.event_type not in VALID_EVENT_TYPES:
            rejected += 1
            logger.warning("event_rejected_invalid_type", extra={"event_type": event.event_type})
            continue

        if event.event_type in PRODUCT_REQUIRED_TYPES:
            if event.product_id is None or event.product_id not in existing_product_ids:
                rejected += 1
                logger.warning(
                    "event_rejected_missing_product",
                    extra={"event_type": event.event_type, "product_id": event.product_id},
                )
                continue

        rows.append(
            UserEvent(
                user_id=user_id,
                session_id=event.session_id,
                event_type=event.event_type,
                product_id=event.product_id,
                event_metadata=event.metadata,
                client_event_id=event.client_event_id,
            )
        )

    if rows:
        db.bulk_save_objects(rows)
        db.commit()

    return IngestResult(accepted=len(rows), duplicates=duplicates, rejected=rejected)


def reconcile_session(db: Session, session_id: str | None, user_id: int) -> int:
    """
    Called right after a session is issued (register or login) — see
    app/auth/routes.py. Attaches every anonymous event from this
    browser's tracking session to the now-known user, which is what
    makes "behavior before registration still counts" (Stage 0,
    Journey 1) actually true rather than aspirational.
    """
    if not session_id:
        return 0
    updated = (
        db.query(UserEvent)
        .filter(UserEvent.session_id == session_id, UserEvent.user_id.is_(None))
        .update({UserEvent.user_id: user_id}, synchronize_session=False)
    )
    db.commit()
    return updated


def list_recent_events(
    db: Session, *, event_type: str | None = None, user_id: int | None = None, limit: int = 100
) -> list[UserEvent]:
    query = db.query(UserEvent).order_by(UserEvent.created_at.desc())
    if event_type:
        query = query.filter(UserEvent.event_type == event_type)
    if user_id is not None:
        # Stage 14: the admin "behavior & recommendations" view (Journey
        # 3) needs one user's own event history alongside their
        # recommendation trace — same underlying query /admin/events
        # already made, just scoped down rather than duplicated.
        query = query.filter(UserEvent.user_id == user_id)
    return query.limit(limit).all()


def count_events(db: Session) -> int:
    return db.query(UserEvent).count()
