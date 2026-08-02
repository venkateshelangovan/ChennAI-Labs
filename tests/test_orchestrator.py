"""
Stage 11's critical path: the bounded retrieval-quality-gate-and-refine
loop in app/recommendations/orchestrator.py. Two layers of tests, same
split as app/recommendations/orchestrator.py's own design:

1. The quality gate and query-narrowing themselves (`_assess_quality`,
   `_narrow_profile`) — pure functions over synthetic data, no DB, no
   vector store.
2. `retrieve_with_refinement`'s orchestration logic (does it refine when
   it should, stop when quality is fine, stop after MAX_REFINE_ATTEMPTS
   even if still weak, skip refinement entirely for a cold-start
   result) — tested by monkeypatching `retrieve_for_profile` itself
   (the one seam this module calls through) with scripted responses,
   the same "replace the seam, not the real vector store" approach
   test_mesh_client.py uses for httpx. This keeps these tests fast and
   exact about what triggers a retry, rather than depending on the
   placeholder embedder's specific similarity numbers for a real
   catalog.
"""

from datetime import datetime

import pytest

from app.profile.schemas import CategoryScore, InterestProfile, ProductAffinity, SearchTerm, TagScore
from app.recommendations import orchestrator as orch
from app.retrieval.query import RetrievalCandidate, RetrievalResult

NOW = datetime(2026, 1, 1)


def _profile(**overrides):
    defaults = dict(
        user_id=1,
        generated_at=NOW,
        sample_size=10,
        category_scores=[
            CategoryScore(category="Agentic AI", score=0.7, evidence_count=5),
            CategoryScore(category="RAG", score=0.3, evidence_count=2),
        ],
        tag_scores=[
            TagScore(tag="agents", score=0.6, evidence_count=4),
            TagScore(tag="rag", score=0.4, evidence_count=2),
        ],
        top_products=[
            ProductAffinity(
                product_id=99, slug="some-course", title="Some Course", category="Agentic AI",
                relative_score=1.0, evidence_count=3,
            )
        ],
        search_terms=[SearchTerm(query="agentic ai systems", count=2, last_searched_at=NOW)],
    )
    defaults.update(overrides)
    return InterestProfile(**defaults)


def _candidate(product_id, similarity, category="Agentic AI"):
    return RetrievalCandidate(
        product_id=product_id,
        slug=f"course-{product_id}",
        title=f"Course {product_id}",
        category=category,
        distance=round(2 - 2 * similarity, 4),
        similarity=similarity,
    )


def _result(candidates, *, query_text="q", is_cold_start=False):
    return RetrievalResult(
        user_id=1, generated_at=NOW, query_text=query_text, is_cold_start=is_cold_start, candidates=candidates
    )


# ---------------------------------------------------------------------------
# _assess_quality
# ---------------------------------------------------------------------------

def test_assess_quality_no_candidates_is_weak():
    ok, reason = orch._assess_quality(_result([]))
    assert ok is False
    assert reason == "no_candidates"


def test_assess_quality_too_few_candidates_is_weak():
    candidates = [_candidate(i, 0.9) for i in range(orch.MIN_CANDIDATES - 1)]
    ok, reason = orch._assess_quality(_result(candidates))
    assert ok is False
    assert reason == "too_few_candidates"


def test_assess_quality_weak_top_similarity_is_weak():
    candidates = [_candidate(i, orch.MIN_TOP_SIMILARITY - 0.01) for i in range(orch.MIN_CANDIDATES)]
    ok, reason = orch._assess_quality(_result(candidates))
    assert ok is False
    assert reason == "weak_top_similarity"


def test_assess_quality_ok_when_enough_candidates_and_strong_top_match():
    candidates = [_candidate(0, 0.9)] + [_candidate(i, 0.5) for i in range(1, orch.MIN_CANDIDATES)]
    ok, reason = orch._assess_quality(_result(candidates))
    assert ok is True
    assert reason == "ok"


# ---------------------------------------------------------------------------
# _narrow_profile
# ---------------------------------------------------------------------------

def test_narrow_profile_keeps_only_strongest_category_and_tag():
    profile = _profile()
    narrowed = orch._narrow_profile(profile)

    assert [c.category for c in narrowed.category_scores] == ["Agentic AI"]
    assert [t.tag for t in narrowed.tag_scores] == ["agents"]


def test_narrow_profile_drops_top_products_and_search_terms():
    profile = _profile()
    narrowed = orch._narrow_profile(profile)

    assert narrowed.top_products == []
    assert narrowed.search_terms == []


def test_narrow_profile_leaves_original_profile_untouched():
    profile = _profile()
    orch._narrow_profile(profile)
    assert len(profile.category_scores) == 2  # the input wasn't mutated
    assert len(profile.tag_scores) == 2


def test_narrow_profile_handles_a_profile_with_no_categories_or_tags():
    profile = _profile(category_scores=[], tag_scores=[])
    narrowed = orch._narrow_profile(profile)
    assert narrowed.category_scores == []
    assert narrowed.tag_scores == []


# ---------------------------------------------------------------------------
# retrieve_with_refinement — orchestration over a stubbed retrieve_for_profile
# ---------------------------------------------------------------------------

def test_no_refinement_when_first_attempt_is_already_good(monkeypatch):
    good = _result([_candidate(0, 0.9)] + [_candidate(i, 0.5) for i in range(1, orch.MIN_CANDIDATES)])
    calls = []

    def fake_retrieve(db, profile, *, top_k):
        calls.append(profile)
        return good

    monkeypatch.setattr(orch, "retrieve_for_profile", fake_retrieve)

    outcome = orch.retrieve_with_refinement(None, _profile(), top_k=30)

    assert len(calls) == 1
    assert outcome.refined is False
    assert outcome.result is good
    assert len(outcome.attempts) == 1
    assert outcome.attempts[0].quality_ok is True


def test_refines_once_when_first_attempt_is_weak_then_succeeds(monkeypatch):
    weak = _result([_candidate(0, 0.05)])  # both too few AND weak top similarity
    strong = _result([_candidate(0, 0.8)] + [_candidate(i, 0.5) for i in range(1, orch.MIN_CANDIDATES)])
    responses = [weak, strong]
    seen_profiles = []

    def fake_retrieve(db, profile, *, top_k):
        seen_profiles.append(profile)
        return responses.pop(0)

    monkeypatch.setattr(orch, "retrieve_for_profile", fake_retrieve)

    outcome = orch.retrieve_with_refinement(None, _profile(), top_k=30)

    assert len(seen_profiles) == 2
    assert outcome.refined is True
    assert outcome.result is strong
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].quality_ok is False
    assert outcome.attempts[1].quality_ok is True
    # the second call really did happen with the narrowed profile
    assert len(seen_profiles[1].category_scores) == 1
    assert len(seen_profiles[0].category_scores) == 2


def test_stops_after_max_refine_attempts_even_if_still_weak(monkeypatch):
    weak = _result([_candidate(0, 0.05)])
    calls = []

    def fake_retrieve(db, profile, *, top_k):
        calls.append(profile)
        return weak

    monkeypatch.setattr(orch, "retrieve_for_profile", fake_retrieve)

    outcome = orch.retrieve_with_refinement(None, _profile(), top_k=30)

    assert len(calls) == orch.MAX_REFINE_ATTEMPTS + 1
    assert outcome.refined is True
    assert all(a.quality_ok is False for a in outcome.attempts)
    assert outcome.result is weak  # gives up gracefully, hands back what it found


def test_keeps_the_better_of_two_weak_attempts(monkeypatch):
    weaker = _result([_candidate(0, 0.05)])
    less_weak = _result([_candidate(0, 0.05), _candidate(1, 0.05)])  # more candidates, still below the bar
    responses = [weaker, less_weak]

    def fake_retrieve(db, profile, *, top_k):
        return responses.pop(0)

    monkeypatch.setattr(orch, "retrieve_for_profile", fake_retrieve)

    outcome = orch.retrieve_with_refinement(None, _profile(), top_k=30)

    assert outcome.result is less_weak  # more candidates wins the tiebreak


def test_cold_start_retrieval_result_skips_refinement(monkeypatch):
    cold = _result([], query_text="", is_cold_start=True)
    calls = []

    def fake_retrieve(db, profile, *, top_k):
        calls.append(profile)
        return cold

    monkeypatch.setattr(orch, "retrieve_for_profile", fake_retrieve)

    outcome = orch.retrieve_with_refinement(None, _profile(), top_k=30)

    assert len(calls) == 1  # never tries to refine a query that doesn't exist
    assert outcome.refined is False
    assert outcome.result is cold
