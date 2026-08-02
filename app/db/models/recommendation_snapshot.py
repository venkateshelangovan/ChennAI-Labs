"""
Stage 12: the persisted cache Section 15 of the Stage 0 doc describes —
"the stored recommendations row *is* the cache, served as-is until the
trigger says otherwise." Before this table existed, every single
`/dashboard` view recomputed Stage 7-11's full retrieval pipeline AND
made a real Mesh chat call for narration (Stage 10) — correct in
isolation, but exactly the AI-cost-proportional-to-page-views anti-
pattern Section 14 warns against. A user who reloads their dashboard
five times in a minute has not generated five times as much genuine
behavioral signal.

One row per user (`user_id` is unique, not a history table) — Stage 14
is where an audit trail / trace blob is planned; until then, "the
current cached recommendation" is all this needs to represent, and a
history table with no reader would just be unused rows accumulating
forever.

What's cached vs. what's always read live: `recommendations` stores
only the RANKING DECISION — which product IDs, in what order, with
what reason and similarity score, exactly the fields
app/recommendations/schemas.py's Recommendation doesn't already get by
wrapping a live Product row. Display fields (price, rating, title, ...)
are deliberately NOT duplicated here — app/recommendations/cache.py
re-fetches the real Product rows by ID every time a cached snapshot is
served, so a price or rating change is never stale just because the
recommendation itself hasn't changed. If a cached product ID no longer
resolves to an active product (archived since the snapshot was taken),
that's the same "vector index ahead of SQL" drift-safety case Stage 7/8
already handle — see app/recommendations/cache.py.

`narration_*` mirrors app/recommendations/narration.py's NarrationResult
so a cache hit doesn't have to re-derive whether the narration is safe
to show — the earlier grounding validation already decided that once,
at generation time.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class RecommendationSnapshot(Base):
    __tablename__ = "recommendation_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True
    )

    generated_at: Mapped[datetime] = mapped_column(nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    retrieval_refined: Mapped[bool] = mapped_column(nullable=False, default=False)

    # list[{"product_id": int, "reason": str, "similarity": float | None}],
    # in final display order — see app/recommendations/cache.py for the
    # (de)serialization boundary.
    recommendations: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)

    narration_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    narration_grounded: Mapped[bool] = mapped_column(nullable=False, default=False)
    narration_fallback_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
