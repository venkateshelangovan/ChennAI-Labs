"""
Stage 12: the top of the recommendation stack — the one function
`/dashboard` actually calls now. Everything below it (Stage 7's
retrieval, Stage 11's refinement loop, Stage 8's novelty/diversity/
fallback, Stage 10's Mesh narration) is unchanged; this module decides
whether any of that needs to run at all, using app/recommendations/
trigger.py's deterministic decision, then persists whatever it computes
so the next call has something to serve without recomputing.

--- What's cached, what's always live ---

`RecommendationSnapshot` (app/db/models/recommendation_snapshot.py)
stores the RANKING decision — product IDs, order, reason, similarity —
never display fields. `_load_from_snapshot` re-fetches the real
`Product` rows by ID on every cache hit, so a price or rating change is
never stale just because the recommendation itself hasn't changed.

If a cached product ID no longer resolves (archived since the snapshot
was generated), it's silently dropped — same drift-safety pattern
Stage 7/8 already use. If EVERY cached product has vanished, a
snapshot that resolves to zero recommendations is treated as if it
didn't exist at all: falling through to a fresh `generate_recommendations`
call is strictly better than showing an honestly-empty "recommended for
you" section when the underlying catalog has moved on.

--- Why generation still happens synchronously, in-request ---

Stage 0 Section 15 describes a "stale-while-revalidate" pattern where a
stale recommendation is served immediately and regeneration happens in
the background via FastAPI `BackgroundTasks`. That's explicitly a
Stage 15 concern (the proactive daily digest) — through Stage 14, the
same section says generation runs synchronously within the triggering
request, because traffic is low and a few seconds of latency behind a
loading state is an acceptable, honest cost for a page that's about to
show real Mesh-generated content. Stage 12 only decides WHETHER to
pay that cost on a given request, not how to hide it — that's next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.product import Product
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.recommendations import trigger
from app.recommendations.narration import NarrationResult, generate_narration
from app.recommendations.schemas import Recommendation, RecommendationResult
from app.recommendations.service import generate_recommendations


@dataclass
class DashboardRecommendations:
    result: RecommendationResult
    narration: NarrationResult
    cache_hit: bool
    trigger_reason: str


def _serialize(result: RecommendationResult) -> list[dict]:
    return [
        {"product_id": r.product.id, "reason": r.reason, "similarity": r.similarity}
        for r in result.recommendations
    ]


def _load_from_snapshot(
    db: Session, snapshot: RecommendationSnapshot, user_id: int
) -> tuple[RecommendationResult, NarrationResult] | None:
    product_ids = [row["product_id"] for row in snapshot.recommendations]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}

    recommendations = []
    for row in snapshot.recommendations:
        product = products.get(row["product_id"])
        if product is None or not product.is_active:
            continue  # archived/removed since the snapshot was generated
        recommendations.append(
            Recommendation(product=product, reason=row["reason"], similarity=row["similarity"])
        )

    if snapshot.recommendations and not recommendations:
        # Every cached product vanished — this snapshot no longer means
        # anything real. Treat it as a cache miss rather than showing an
        # honestly-empty list when a fresh call could do better.
        return None

    result = RecommendationResult(
        user_id=user_id,
        generated_at=snapshot.generated_at,
        strategy=snapshot.strategy,
        recommendations=recommendations,
        retrieval_refined=snapshot.retrieval_refined,
    )
    narration = NarrationResult(
        text=snapshot.narration_text,
        grounded=snapshot.narration_grounded,
        fallback_reason=snapshot.narration_fallback_reason,
    )
    return result, narration


def _persist(db: Session, user_id: int, now: datetime, result: RecommendationResult, narration: NarrationResult) -> None:
    snapshot = db.query(RecommendationSnapshot).filter(RecommendationSnapshot.user_id == user_id).one_or_none()
    if snapshot is None:
        snapshot = RecommendationSnapshot(user_id=user_id)
        db.add(snapshot)

    snapshot.generated_at = now
    snapshot.strategy = result.strategy
    snapshot.retrieval_refined = result.retrieval_refined
    snapshot.recommendations = _serialize(result)
    snapshot.narration_text = narration.text
    snapshot.narration_grounded = narration.grounded
    snapshot.narration_fallback_reason = narration.fallback_reason
    db.commit()


def get_dashboard_recommendations(
    db: Session, user_id: int, *, manual_refresh: bool = False, now: datetime | None = None
) -> DashboardRecommendations:
    now = now or utcnow()
    snapshot = db.query(RecommendationSnapshot).filter(RecommendationSnapshot.user_id == user_id).one_or_none()

    decision = trigger.evaluate(db, snapshot, user_id, manual=manual_refresh, now=now)

    if not decision.should_refresh and snapshot is not None:
        loaded = _load_from_snapshot(db, snapshot, user_id)
        if loaded is not None:
            result, narration = loaded
            return DashboardRecommendations(
                result=result, narration=narration, cache_hit=True, trigger_reason=decision.reason
            )
        # Cached products all vanished — fall through and regenerate,
        # same as a "no_snapshot" cache miss.
        decision = trigger.TriggerDecision(True, "cached_products_gone")

    result = generate_recommendations(db, user_id, now=now)
    narration = generate_narration(result.recommendations)
    _persist(db, user_id, now, result, narration)

    return DashboardRecommendations(result=result, narration=narration, cache_hit=False, trigger_reason=decision.reason)
