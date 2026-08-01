"""
The rawest layer of behavioral truth (Stage 0, Section 10). Append-only
— nothing in the app ever updates or deletes a row here except the one
reconciliation UPDATE that attaches anonymous events to a newly
registered/logged-in user (see app/events/service.py).

Why `user_id` is nullable: the catalog (Stage 3) is public — a visitor
browses before they ever register. Their behavior is still worth
capturing (it's exactly what Stage 0's "Journey 1" describes: pre-
registration behavior gets attached to the account once they sign up).
`session_id` is what makes that possible — every browser gets one,
authenticated or not, generated client-side by app/static/js/tracker.js
and stored in a first-party cookie.

Why `client_event_id`: sendBeacon has no built-in retry/response
handling, but a flaky network can still cause a browser to legitimately
attempt delivering the same beacon twice, and the challenge explicitly
calls out testing duplicate-event handling. Rather than trying to
detect duplicates heuristically, the client mints a UUID per event and
the server deduplicates on it — a real idempotency key, not a guess.

Why `product_id` has ON DELETE SET NULL rather than CASCADE: products
are only ever soft-deleted (archived) in this app, never hard-deleted,
but if that ever changes, behavioral history for a since-removed
product is still meaningful for understanding a user's interests — it
should outlive the product row, not vanish with it.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base

VALID_EVENT_TYPES = ("view", "search", "click", "category_view", "time_spent")


class UserEvent(Base):
    __tablename__ = "user_events"
    __table_args__ = (
        Index("ix_user_events_user_created", "user_id", "created_at"),
        Index("ix_user_events_session_created", "session_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    client_event_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)
