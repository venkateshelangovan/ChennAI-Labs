"""
Stage 11: the one genuinely cyclic decision in this pipeline —
"if the retrieved candidates look weak, refine the query and retry" —
and the explicit LangGraph-vs-plain-Python call the Stage 0 architecture
document (Section 12) deliberately deferred to this stage rather than
pre-committing to either answer.

--- The decision: no LangGraph, at least not yet ---

The full agentic-workflow flowchart (Stage 0, Section 12) has exactly
ONE cyclic edge: RET -> QUAL -> REFINE -> RET. Everything else in the
pipeline (trigger check, ranking, generation, grounding validation) is
already a straight line, built stage by stage from Stage 6 through
Stage 10. A single conditional retry, bounded to one extra attempt, is
not the kind of workflow graph machinery earns its keep on: LangGraph's
actual value is in coordinating multiple interacting branches, multi-
agent handoff, human-in-the-loop interrupts, or state that needs to
survive across process boundaries — none of which apply here. What we
have is a `for` loop with a `break` condition, and writing it as a
5-line loop is not just simpler than a 2-node graph, it's *more*
observable in this codebase specifically, because every other stage
already logs structured JSON that answers "why did this happen" from
log lines alone (Stage 0, Section 17) — introducing a second tracing
mechanism (LangSmith) for exactly one loop would be adding
infrastructure ahead of a need, which is the same anti-pattern this
project has explicitly avoided for Redis (Section 15) and Celery
(Section 15) elsewhere.

This isn't a permanent rejection of LangGraph — it's the "linear
version already working as the fallback" the architecture doc
anticipated, chosen because it's still true today. Revisit this call if
a later stage introduces a workflow with real branching (e.g. multiple
distinct refinement strategies competing, or a step that needs to pause
for human review before continuing) — that's the shape of problem a
graph actually clarifies over nested conditionals.

--- What "weak retrieval" means, deterministically ---

Two signals, both computed from Stage 7's raw RetrievalResult, before
Stage 8's novelty/diversity filtering ever runs (a query that returns
plenty of *strong* matches the user already engaged with is not a
retrieval-quality problem — that's exactly what Stage 8's novelty
filter and popularity fallback already handle correctly; refining the
query wouldn't fix "the top matches happen to be things you already
saw," and re-querying for that reason would just burn an extra vector
search for no benefit):

1. TOO FEW CANDIDATES — fewer than MIN_CANDIDATES raw hits came back
   at all. Below this, there isn't enough in the pool for Stage 8's
   diversity cap to do meaningful work regardless of how it's filtered.
2. WEAK TOP MATCH — even the single closest candidate's similarity is
   below MIN_TOP_SIMILARITY. If the *best* match is that far away, the
   query text itself likely diluted a real signal with noise (e.g. a
   long tail of low-weight tags, or stale search terms) rather than the
   catalog genuinely lacking anything relevant.

--- The refinement strategy ---

`_narrow_profile` rebuilds a stripped-down copy of the same
InterestProfile — keeping only the single strongest category and
single strongest tag (both lists are already sorted by score
descending, see app/profile/service.py), dropping top_products and
search_terms entirely. This is a genuinely different, sharper query:
Stage 7's `_profile_to_query_text` synthesizes query text by walking
every category, every tag, every top product title, and every search
term, so a profile with a lot of low-weight noise around one strong
signal produces a query text where that signal is a minority of the
tokens. Narrowing to just the strongest category/tag removes that
dilution without inventing anything — every token in the refined query
still comes directly from the user's real profile, just less of it.

Bounded to MAX_REFINE_ATTEMPTS (1) — exactly the flowchart's one
loop-back edge, not an open-ended retry. If the refined query is still
weak, the orchestrator gives up gracefully and hands whichever attempt
scored better to Stage 8, which already has its own robust fallback
(popularity ranking) for exactly this "nothing good was found" case.
Stage 11's job is only to give the deterministic pipeline downstream
the best shot at working with something real — never to guarantee a
strong match exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from sqlalchemy.orm import Session

from app.profile.schemas import InterestProfile
from app.retrieval.query import RetrievalResult, retrieve_for_profile

logger = logging.getLogger("chennai_labs.recommendations.orchestrator")

MIN_CANDIDATES = 5
MIN_TOP_SIMILARITY = 0.15
MAX_REFINE_ATTEMPTS = 1


@dataclass
class RetrievalAttempt:
    attempt: int  # 0 = original query, 1+ = refined retries
    refined: bool
    query_text: str
    candidate_count: int
    top_similarity: float | None
    quality_ok: bool
    reason: str  # "ok" | "no_candidates" | "too_few_candidates" | "weak_top_similarity"


@dataclass
class OrchestratedRetrieval:
    result: RetrievalResult  # the winning attempt — handed to Stage 8 exactly like a Stage-7-only call would be
    attempts: list[RetrievalAttempt] = field(default_factory=list)

    @property
    def refined(self) -> bool:
        return len(self.attempts) > 1


def _assess_quality(result: RetrievalResult) -> tuple[bool, str]:
    if not result.candidates:
        return False, "no_candidates"
    if len(result.candidates) < MIN_CANDIDATES:
        return False, "too_few_candidates"
    if result.candidates[0].similarity < MIN_TOP_SIMILARITY:
        return False, "weak_top_similarity"
    return True, "ok"


def _narrow_profile(profile: InterestProfile) -> InterestProfile:
    """
    A sharper, noise-stripped copy of the same profile: only the single
    strongest category and tag, no top-product titles, no search terms.
    Both `category_scores` and `tag_scores` are pre-sorted descending by
    `build_interest_profile` (app/profile/service.py), so `[:1]` is
    exactly "this user's single strongest signal."
    """
    return replace(
        profile,
        category_scores=profile.category_scores[:1],
        tag_scores=profile.tag_scores[:1],
        top_products=[],
        search_terms=[],
    )


def _score_of(attempt_result: RetrievalResult) -> tuple[int, float]:
    """Ranks a retrieval attempt for "which one do we hand off downstream"
    when every attempt was exhausted without hitting the quality bar —
    more candidates first, then the strongest top match, so the
    orchestrator always keeps the best evidence it actually found rather
    than defaulting to whichever attempt happened to run last."""
    if not attempt_result.candidates:
        return (0, 0.0)
    return (len(attempt_result.candidates), attempt_result.candidates[0].similarity)


def retrieve_with_refinement(
    db: Session, profile: InterestProfile, *, top_k: int
) -> OrchestratedRetrieval:
    """
    Stage 7's `retrieve_for_profile`, wrapped in the one bounded
    refine-and-retry loop the Stage 0 flowchart calls for. Cold-start
    profiles are returned as-is on the first attempt — there's no query
    text to refine when there's no signal at all, and Stage 8 already
    knows how to turn a cold-start RetrievalResult into the popularity
    fallback.
    """
    attempts: list[RetrievalAttempt] = []
    best_result = None
    best_score = (-1, -1.0)

    current_profile = profile
    for attempt_number in range(MAX_REFINE_ATTEMPTS + 1):
        result = retrieve_for_profile(db, current_profile, top_k=top_k)
        quality_ok, reason = _assess_quality(result)

        attempts.append(
            RetrievalAttempt(
                attempt=attempt_number,
                refined=attempt_number > 0,
                query_text=result.query_text,
                candidate_count=len(result.candidates),
                top_similarity=result.candidates[0].similarity if result.candidates else None,
                quality_ok=quality_ok,
                reason=reason,
            )
        )
        logger.info(
            "retrieval_quality_checked",
            extra={
                "user_id": profile.user_id,
                "attempt": attempt_number,
                "refined": attempt_number > 0,
                "candidate_count": len(result.candidates),
                "top_similarity": result.candidates[0].similarity if result.candidates else None,
                "quality_ok": quality_ok,
                "reason": reason,
            },
        )

        score = _score_of(result)
        if score > best_score:
            best_score = score
            best_result = result

        if result.is_cold_start or quality_ok or attempt_number == MAX_REFINE_ATTEMPTS:
            break

        logger.info("retrieval_refining", extra={"user_id": profile.user_id, "next_attempt": attempt_number + 1})
        current_profile = _narrow_profile(current_profile)

    return OrchestratedRetrieval(result=best_result, attempts=attempts)
