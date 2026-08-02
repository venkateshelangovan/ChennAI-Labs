"""
Stage 8: the deterministic recommendation pipeline — turning Stage 7's
raw retrieval candidates into the actual "Recommended for you" list a
user sees on /dashboard. Still no AI (Mesh arrives Stage 9); everything
here is business logic layered on top of retrieval, in three parts:

1. NOVELTY — exclude products the user has already directly engaged
   with (viewed, clicked, or spent time on). Stage 7's retrieval query
   is built out of exactly that engagement, so without this filter the
   #1 "recommendation" is routinely the very course the user just
   clicked — a correct nearest neighbor, but not a useful
   recommendation. A discovery list should point at what the user
   HASN'T seen yet that looks like what they have.

2. DIVERSITY — cap how many results can come from the same category
   (`MAX_PER_CATEGORY`), applied as a greedy re-rank over candidates
   already sorted by similarity (`_diversify`). Without this, a user
   who watched one RAG video gets a top-6 list that's 6 RAG courses —
   the closest matches individually, but a worse list than a spread
   across their actual (broader) interest profile. The cap is relaxed
   only on a second pass if there aren't enough remaining candidates to
   fill the list otherwise — it's a preference for variety, not a hard
   rule worth returning fewer than `top_n` results to satisfy.

3. COLD START — a user with no behavioral signal (or, after novelty
   filtering, no *novel* candidates left) gets a deterministic,
   non-personalized fallback: the highest-rated active courses,
   diversified the same way, rather than an empty list or an error.
   This is the same "never divide by a zero total" principle Stage 6/7
   already established for the profile and retrieval preview — Stage 8
   is where it actually produces something the user sees.

Every recommendation carries a `reason` string. These are NOT generated
text — they're one of two fixed templates, filled in with a real fact
about the candidate (its own category) or an honest statement that
there isn't enough data yet. Nothing here writes prose *about* the
user; that's exactly the kind of claim only a real language model
(Stage 9+, and even then carefully) should be trusted to make.

Stage 11 inserts one thing ahead of novelty/diversity: Stage 7's raw
retrieval now goes through `app/recommendations/orchestrator.py`'s
bounded quality-gate-and-refine loop before this function ever sees it.
Nothing below this point changed to accommodate that — `outcome.result`
is a plain `RetrievalResult`, exactly what `retrieve_for_profile`
always returned, so novelty/diversity/fallback logic didn't need to
know refinement happened at all. See that module's docstring for the
full design (what "weak retrieval" means, the refinement strategy, and
why this stayed a plain Python loop instead of adopting LangGraph).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Callable, TypeVar

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.product import Product
from app.db.models.user_event import UserEvent
from app.profile.service import build_interest_profile
from app.recommendations.orchestrator import OrchestratedRetrieval, retrieve_with_refinement
from app.recommendations.schemas import Recommendation, RecommendationResult
from app.retrieval.query import RetrievalCandidate

DEFAULT_TOP_N = 6
CANDIDATE_POOL_SIZE = 30  # fetched from the vector store — more than top_n so novelty + diversity have room to filter
MAX_PER_CATEGORY = 2

POPULAR_FALLBACK_REASON = "Popular pick — we don't have enough of your activity yet for personalized picks."

T = TypeVar("T")


def _serialize_candidates(candidates: list[RetrievalCandidate]) -> list[dict]:
    return [
        {"product_id": c.product_id, "title": c.title, "category": c.category, "similarity": c.similarity}
        for c in candidates
    ]


def _trace(path: str, *, outcome: OrchestratedRetrieval | None = None, **extra) -> dict:
    """
    Stage 14: one small helper so every return path in
    generate_recommendations builds its trace dict the same shape —
    `path` names which of the branches below produced this result
    (mirrors Section 17's "why did this user get this recommendation,"
    answerable without reading code). `outcome`, when given, contributes
    Stage 11's full retrieval-attempt history and the winning attempt's
    raw candidate pool (pre-novelty, pre-diversity) with real similarity
    scores — everything else is branch-specific (engaged IDs excluded,
    counts before/after filtering, etc.), passed in via **extra.
    """
    trace = {"path": path}
    if outcome is not None:
        trace["retrieval_attempts"] = [asdict(a) for a in outcome.attempts]
        trace["candidate_pool"] = _serialize_candidates(outcome.result.candidates)
    trace.update(extra)
    return trace


def _engaged_product_ids(db: Session, user_id: int) -> set[int]:
    rows = (
        db.query(UserEvent.product_id)
        .filter(UserEvent.user_id == user_id, UserEvent.product_id.isnot(None))
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def _diversify(items: list[T], top_n: int, *, category_of: Callable[[T], str]) -> list[T]:
    """
    Greedy re-rank: walk `items` in their existing order (similarity-
    or rating-sorted), taking each one unless its category has already
    hit MAX_PER_CATEGORY, until `top_n` is filled. If the cap leaves
    the list short — e.g. the candidate pool is dominated by two or
    three categories — a second pass fills remaining slots from
    whatever was skipped, in original order, rather than returning
    fewer than `top_n`.
    """
    if top_n <= 0:
        return []

    counts: dict[str, int] = {}
    picked: list[T] = []
    skipped: list[T] = []

    for item in items:
        category = category_of(item)
        if counts.get(category, 0) < MAX_PER_CATEGORY:
            picked.append(item)
            counts[category] = counts.get(category, 0) + 1
            if len(picked) == top_n:
                return picked
        else:
            skipped.append(item)

    for item in skipped:
        picked.append(item)
        if len(picked) == top_n:
            break

    return picked


def _popular_fallback(
    db: Session,
    *,
    user_id: int,
    now: datetime,
    top_n: int,
    exclude_ids: frozenset[int] = frozenset(),
    trace: dict | None = None,
) -> RecommendationResult:
    """
    Deterministic, non-personalized ranking: highest-rated active
    courses first, ties broken by id for stability across calls.
    `.nullslast()` is explicit (not relied on as SQLite's default
    behavior) because SQLite and Postgres disagree on where NULLs sort
    in a DESC ordering by default — this app moves from one to the
    other (Stage 0), so an implicit "works on my SQLite" ordering would
    be exactly the kind of thing that quietly changes behavior in
    production.
    """
    query = db.query(Product).filter(Product.status == "active")
    if exclude_ids:
        query = query.filter(~Product.id.in_(exclude_ids))
    ordered = query.order_by(Product.rating.desc().nullslast(), Product.id.asc()).all()

    diversified = _diversify(ordered, top_n, category_of=lambda p: p.category)
    recommendations = [
        Recommendation(product=p, reason=POPULAR_FALLBACK_REASON, similarity=None) for p in diversified
    ]
    return RecommendationResult(
        user_id=user_id,
        generated_at=now,
        strategy="popular_fallback",
        recommendations=recommendations,
        trace=trace or {"path": "popular_fallback"},
    )


def generate_recommendations(
    db: Session, user_id: int, *, top_n: int = DEFAULT_TOP_N, now: datetime | None = None
) -> RecommendationResult:
    now = now or utcnow()
    profile = build_interest_profile(db, user_id, now=now)

    if profile.is_cold_start:
        return _popular_fallback(
            db, user_id=user_id, now=now, top_n=top_n,
            trace=_trace("cold_start", reason="no_behavioral_signal_yet"),
        )

    outcome = retrieve_with_refinement(db, profile, top_k=CANDIDATE_POOL_SIZE)
    retrieval = outcome.result
    if not retrieval.candidates:
        return _popular_fallback(
            db, user_id=user_id, now=now, top_n=top_n,
            trace=_trace("no_retrieval_candidates", outcome=outcome),
        )

    engaged_ids = _engaged_product_ids(db, user_id)
    novel_candidates = [c for c in retrieval.candidates if c.product_id not in engaged_ids]
    if not novel_candidates:
        # Every retrieval candidate is something the user already engaged
        # with — still exclude those from the fallback too, rather than
        # turning around and recommending the exact course they just
        # clicked because the "personalized" path came up empty.
        return _popular_fallback(
            db, user_id=user_id, now=now, top_n=top_n, exclude_ids=frozenset(engaged_ids),
            trace=_trace(
                "all_candidates_already_engaged", outcome=outcome,
                engaged_product_ids=sorted(engaged_ids),
            ),
        )

    diversified = _diversify(novel_candidates, top_n, category_of=lambda c: c.category)

    products = {
        p.id: p
        for p in db.query(Product).filter(Product.id.in_([c.product_id for c in diversified])).all()
    }
    recommendations = []
    for candidate in diversified:
        product = products.get(candidate.product_id)
        if product is None:
            continue  # vector index ahead of SQL — same drift-safety pattern as app/retrieval/query.py
        recommendations.append(
            Recommendation(
                product=product,
                reason=f"Matches your recent interest in {candidate.category}",
                similarity=candidate.similarity,
            )
        )

    return RecommendationResult(
        user_id=user_id,
        generated_at=now,
        strategy="personalized",
        recommendations=recommendations,
        retrieval_refined=outcome.refined,
        trace=_trace(
            "personalized",
            outcome=outcome,
            engaged_product_ids=sorted(engaged_ids),
            novel_candidate_count=len(novel_candidates),
            final_recommendation_count=len(recommendations),
        ),
    )
