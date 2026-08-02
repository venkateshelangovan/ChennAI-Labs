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

One row per user (`user_id` is unique, not a history table) — an audit
TRAIL (every past recommendation ever generated) isn't what Stage 14
builds; the ask (Stage 0 Section 17) is a full TRACE per recommendation
("why did this user get this recommendation," answerable from the row
alone), and the current row already regenerates on every trigger fire,
which is exactly when a fresh trace is worth having. A history table
with no reader would just be unused rows accumulating forever — if a
real audit trail is ever needed, that's a distinct, larger feature than
what Section 17 actually asks for here.

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

Stage 14 adds `trigger_reason` (why THIS generation happened — see
app/recommendations/trigger.py's TriggerDecision) and `trace` (the full
"why did this user get this recommendation" JSON blob —
RecommendationResult.trace plus narration's raw pre-substitution text
and any rejected citation indices — assembled by
app/recommendations/cache.py). Both are admin-only: the /admin
"behavior & recommendations" view (Journey 3) reads them directly off
this row, exactly as Section 17 describes, rather than trying to
reconstruct anything after the fact from logs that eventually rotate.
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

    # Stage 14 — observability, admin-only (never read by /dashboard).
    trigger_reason: Mapped[str] = mapped_column(String(40), nullable=False, default="no_snapshot")
    trace: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
