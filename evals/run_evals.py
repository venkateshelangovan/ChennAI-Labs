"""
Stage 17: the eval framework.

--- What this is, and what it deliberately is NOT ---

pytest (198 tests through Stage 16, extended in this stage) proves the
pipeline's CODE is correct against exact, hand-picked inputs — a
synthetic event sequence produces exactly the trigger decision / trace
fields / grounding verdict a unit test asserts. That's necessary but
answers a narrower question than "is this a good recommendation
system." This script answers a different question: run the real
pipeline (the same `generate_recommendations` / `generate_narration`
every request calls) against the three demo user journeys Stage 0
Section 23 describes, and report BEHAVIORAL quality metrics — does a
RAG-focused user get RAG/agentic-adjacent recommendations, does a
beginner get kept at beginner/intermediate level, does a mid-journey
pivot actually show up in the next recommendation. None of that is a
pass/fail unit-test assertion on internal plumbing; it's closer to
"does this look like a good product," reported as numbers you can
compare run over run.

--- Why offline / no real Mesh call ---

Section 18: "The Mesh client is always a fake/mock in tests — no test
ever calls the real Mesh API — so tests are deterministic and free."
This script keeps that discipline for the same reason: a repeatable
eval you can run in CI or before a demo has to produce the same report
on the same code today and next month, which a live network call to a
metered API can't guarantee (and would cost money on every run for no
added signal — the narration's exact wording isn't what's being
evaluated here, its GROUNDING behavior already has dedicated,
exhaustive pytest coverage in tests/test_narration.py, including
Section 18's "single most important test in the system": a fabricated
citation gets rejected). `embeddings_module.LocalHashEmbeddingProvider`
(Stage 4's deterministic bag-of-words-hash placeholder — real distance
metric, not semantic, but real lexical-overlap signal since it embeds
title/category/tags/description text) stands in for Mesh embeddings,
and a scripted, always-grounded fake stands in for Mesh chat.

--- Metrics computed, and why each one ---

1. Category relevance (Users A & B): what fraction of a user's
   recommended products share the interest profile's own dominant
   category (computed by the same deterministic `build_interest_profile`
   the real pipeline uses — not eyeballed from the journey script).
   Directly tests whether retrieval is actually grounded in behavior,
   not just "did it return 6 rows."
2. Level appropriateness (User B specifically): a beginner-signaled
   user's recommendations should skew beginner/intermediate, not jump
   to advanced material adjacent in topic but wrong in difficulty —
   Section 23's explicit example ("doesn't jump to 'production ML
   systems'").
3. Pivot responsiveness (User C): generate a recommendation right after
   an initial browsing session, then again after a second, topically
   different session simulated further in the future (so Stage 6's
   recency half-life has actually decayed the earlier signal) — report
   the category-set Jaccard overlap between the two. Low overlap is the
   evidence that recency weighting (not just "more events accumulated")
   is what changed the outcome.
4. Grounding rate and latency, across every narration/generation call
   made during the run — a sanity check that the harness itself is
   exercising the real pipeline end-to-end (not a specific claim about
   Mesh's real grounding behavior, which is pytest's job).

Run: `python -m evals.run_evals` from the repo root. Writes a report to
evals/latest_report.md and prints the same content to stdout.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.time import utcnow
from app.db.base import Base, register_models
from app.db.models.product import Product
from app.db.models.user_event import UserEvent
from app.mesh import client as mesh_client
from app.products import service as product_service
from app.profile.service import build_interest_profile
from app.recommendations.narration import generate_narration
from app.recommendations.service import MAX_PER_CATEGORY, generate_recommendations
from app.retrieval import embeddings as embeddings_module
from app.retrieval import vector_store as vs_module
from scripts.seed_products import CATALOG

register_models()

REPORT_PATH = Path(__file__).parent / "latest_report.md"


def _fake_chat(*args, **kwargs) -> str:
    """
    Deterministic stand-in for mesh_client.chat — always cites [1], so
    it's always grounded. See module docstring for why this eval
    doesn't re-exercise grounding/hallucination behavior; that's
    pytest's job (tests/test_narration.py).
    """
    return "Based on your recent activity, [1] looks like a strong next step for you."


def _make_db() -> Session:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_catalog(db: Session) -> dict[str, Product]:
    by_title = {}
    for entry in CATALOG:
        product = product_service.create_product(
            db,
            title=entry["title"],
            description=entry["description"],
            category=entry["category"],
            subcategory=entry["subcategory"],
            price=entry["price"],
            level=entry["level"],
            tags=entry["tags"],
            instructor=entry["instructor"],
            duration_minutes=entry["duration_minutes"],
            image_url=None,
            rating=entry["rating"],
        )
        by_title[entry["title"]] = product
    return by_title


def _event(db, *, user_id, event_type, created_at, product_id=None, metadata=None) -> None:
    db.add(
        UserEvent(
            user_id=user_id,
            session_id="eval",
            event_type=event_type,
            product_id=product_id,
            event_metadata=metadata or {},
            client_event_id=str(uuid.uuid4()),
            created_at=created_at,
        )
    )
    db.commit()


@dataclass
class GenerationRun:
    label: str
    categories: list[str]
    levels: list[str]
    strategy: str
    narration_grounded: bool
    latency_ms: float
    dominant_profile_category: str | None
    trace: dict = field(default_factory=dict)


RUNS: list[GenerationRun] = []


def _generate(db, *, user_id, label, now) -> GenerationRun:
    started = time.perf_counter()
    result = generate_recommendations(db, user_id, now=now)
    narration = generate_narration(result.recommendations)
    latency_ms = (time.perf_counter() - started) * 1000

    profile = build_interest_profile(db, user_id, now=now)
    dominant = profile.category_scores[0].category if profile.category_scores else None

    run = GenerationRun(
        label=label,
        categories=[r.product.category for r in result.recommendations],
        levels=[r.product.level for r in result.recommendations],
        strategy=result.strategy,
        narration_grounded=narration.grounded,
        latency_ms=latency_ms,
        dominant_profile_category=dominant,
        trace=result.trace,
    )
    RUNS.append(run)
    return run


def _dominant_category_hits_diversity_cap(run: GenerationRun) -> tuple[int, int, bool] | None:
    """
    NOT a raw "% of recommendations in the dominant category" — Stage
    8's `_diversify` deliberately caps any one category at
    `MAX_PER_CATEGORY` (2) out of `DEFAULT_TOP_N` (6) results, on
    purpose (see app/recommendations/service.py's module docstring: "a
    user who watched one RAG video gets a top-6 list that's 6 RAG
    courses ... a worse list than a spread across their actual
    interest profile"). A 100%-same-category result would actually be
    a DIVERSITY BUG, not a good outcome. The honest question this
    metric answers instead: did the profile's own dominant category
    reach the cap it's allowed to reach (proof retrieval correctly
    identified it as the strongest signal), rather than being
    under-represented or missing entirely (which WOULD indicate a
    retrieval problem)?
    """
    if not run.categories or run.dominant_profile_category is None:
        return None
    matches = sum(1 for c in run.categories if c == run.dominant_profile_category)

    # The TRUE ceiling isn't just min(MAX_PER_CATEGORY, top_n) — if a
    # user has already engaged with most/all of their favorite
    # category's catalog, novelty exclusion (also Stage 8) can leave
    # fewer than MAX_PER_CATEGORY *novel* candidates in that category
    # to recommend at all, which is correct behavior, not a retrieval
    # miss. Compute the real ceiling from the trace's own candidate
    # pool + engaged-product list (Stage 14's observability data) —
    # the same numbers an admin would see on /admin/recommendations —
    # rather than assuming every category always has room to spare.
    candidate_pool = run.trace.get("candidate_pool", [])
    engaged_ids = set(run.trace.get("engaged_product_ids", []))
    novel_in_category = sum(
        1 for c in candidate_pool if c["category"] == run.dominant_profile_category and c["product_id"] not in engaged_ids
    )
    expected = min(MAX_PER_CATEGORY, novel_in_category, len(run.categories))
    return matches, expected, matches >= expected


def _non_advanced_rate(run: GenerationRun) -> float | None:
    if not run.levels:
        return None
    return sum(1 for lvl in run.levels if lvl != "advanced") / len(run.levels)


def _jaccard(a: list[str], b: list[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a and not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# ---------------------------------------------------------------------------
# The three demo journeys (Stage 0, Section 23)
# ---------------------------------------------------------------------------

def journey_a(db, catalog, now):
    """User A — deepens an existing niche (RAG / agentic AI)."""
    uid = 1
    t0 = now - timedelta(hours=3)
    _event(db, user_id=uid, event_type="search", created_at=t0, metadata={"query": "RAG retrieval augmented generation"})
    _event(db, user_id=uid, event_type="view", created_at=t0 + timedelta(minutes=2),
           product_id=catalog["Retrieval-Augmented Generation (RAG) in Production"].id)
    _event(db, user_id=uid, event_type="click", created_at=t0 + timedelta(minutes=5),
           product_id=catalog["Agentic AI Systems"].id)
    _event(db, user_id=uid, event_type="view", created_at=t0 + timedelta(minutes=6),
           product_id=catalog["Agentic AI Systems"].id)
    _event(db, user_id=uid, event_type="time_spent", created_at=t0 + timedelta(minutes=12),
           product_id=catalog["Agentic AI Systems"].id, metadata={"duration_ms": 220_000})
    return _generate(db, user_id=uid, label="User A (RAG / agentic deep-diver)", now=now)


def journey_b(db, catalog, now):
    """
    User B — a beginner exploring fundamentals.

    Deliberately engages only ONE of the catalog's two "Foundations"
    courses ("Python for Machine Learning"), not both — the catalog has
    exactly two Foundations courses total, and Stage 8's novelty filter
    excludes anything already viewed. Viewing both would leave zero
    NOVEL Foundations candidates for the pipeline to recommend, which
    would make the "did the dominant category get recommended"
    question unanswerable by construction (there'd be nothing left to
    recommend in that category, novelty working exactly as designed) —
    a bad eval, not a bad pipeline. The `category_view` event adds
    Foundations-category signal without touching a second specific
    product, so "Mathematics for Machine Learning" stays a valid novel
    candidate.
    """
    uid = 2
    t0 = now - timedelta(hours=2)
    _event(db, user_id=uid, event_type="search", created_at=t0, metadata={"query": "python basics"})
    _event(db, user_id=uid, event_type="view", created_at=t0 + timedelta(minutes=2),
           product_id=catalog["Python for Machine Learning"].id)
    _event(db, user_id=uid, event_type="category_view", created_at=t0 + timedelta(minutes=4),
           metadata={"category": "Foundations"})
    _event(db, user_id=uid, event_type="view", created_at=t0 + timedelta(minutes=10),
           product_id=catalog["Data Analyst Career Track"].id)
    _event(db, user_id=uid, event_type="time_spent", created_at=t0 + timedelta(minutes=20),
           product_id=catalog["Python for Machine Learning"].id, metadata={"duration_ms": 90_000})
    return _generate(db, user_id=uid, label="User B (beginner)", now=now)


def journey_c(db, catalog, now):
    """User C — pivots mid-journey (data analytics -> backend/system design)."""
    uid = 3

    # Session 1, well in the past relative to `now` so Stage 6's 14-day
    # half-life has meaningfully decayed it by the second generation.
    before_now = now - timedelta(days=20)
    t0 = before_now - timedelta(hours=1)
    _event(db, user_id=uid, event_type="search", created_at=t0, metadata={"query": "data analytics dashboards"})
    _event(db, user_id=uid, event_type="view", created_at=t0 + timedelta(minutes=3),
           product_id=catalog["Data Analyst Career Track"].id)
    _event(db, user_id=uid, event_type="category_view", created_at=t0 + timedelta(minutes=5),
           metadata={"category": "Data"})
    _event(db, user_id=uid, event_type="time_spent", created_at=t0 + timedelta(minutes=8),
           product_id=catalog["Data Analyst Career Track"].id, metadata={"duration_ms": 150_000})
    before = _generate(db, user_id=uid, label="User C — before the pivot", now=before_now)

    # Session 2, close to `now` — the pivot into backend/system design.
    t1 = now - timedelta(hours=1)
    _event(db, user_id=uid, event_type="search", created_at=t1, metadata={"query": "system design backend"})
    _event(db, user_id=uid, event_type="view", created_at=t1 + timedelta(minutes=2),
           product_id=catalog["System Design for Senior Engineering Interviews"].id)
    _event(db, user_id=uid, event_type="click", created_at=t1 + timedelta(minutes=3),
           product_id=catalog["System Design for Senior Engineering Interviews"].id)
    _event(db, user_id=uid, event_type="time_spent", created_at=t1 + timedelta(minutes=10),
           product_id=catalog["System Design for Senior Engineering Interviews"].id, metadata={"duration_ms": 200_000})
    after = _generate(db, user_id=uid, label="User C — after the pivot", now=now)

    return before, after


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "WARN"


def build_report(a: GenerationRun, b: GenerationRun, c_before: GenerationRun, c_after: GenerationRun) -> str:
    a_cap = _dominant_category_hits_diversity_cap(a)
    b_cap = _dominant_category_hits_diversity_cap(b)
    b_nonadv = _non_advanced_rate(b)
    c_overlap = _jaccard(c_before.categories, c_after.categories)

    a_ok = a_cap is not None and a_cap[2]
    b_cap_ok = b_cap is not None and b_cap[2]
    b_level_ok = b_nonadv is not None and b_nonadv >= 0.7
    c_ok = c_overlap <= 0.5

    grounded_rate = sum(1 for r in RUNS if r.narration_grounded) / len(RUNS)
    avg_latency = sum(r.latency_ms for r in RUNS) / len(RUNS)

    lines = []
    lines.append("# ChennAI Labs — recommendation quality eval report")
    lines.append("")
    lines.append(f"Generated by `python -m evals.run_evals`. {len(RUNS)} recommendation generations run.")
    lines.append("")
    lines.append("## Journey A — deepens an existing niche (RAG / agentic AI)")
    lines.append("")
    lines.append(f"- Interest profile's dominant category: **{a.dominant_profile_category}**")
    lines.append(f"- Recommended categories: {a.categories}")
    lines.append(
        f"- Dominant category hit its true ceiling ({a_cap[0]}/{a_cap[1]}, where the ceiling is "
        f"min(`MAX_PER_CATEGORY={MAX_PER_CATEGORY}`, novel candidates actually available in that category)): "
        f"**{_verdict(a_ok)}**"
    )
    lines.append(f"- Strategy: {a.strategy}")
    lines.append("")
    lines.append("## Journey B — beginner exploring fundamentals")
    lines.append("")
    lines.append(f"- Interest profile's dominant category: **{b.dominant_profile_category}**")
    lines.append(f"- Recommended categories: {b.categories}")
    lines.append(f"- Recommended levels: {b.levels}")
    lines.append(
        f"- Dominant category hit its true ceiling ({b_cap[0]}/{b_cap[1]}): **{_verdict(b_cap_ok)}**"
    )
    lines.append(f"- Non-advanced rate: **{_fmt_pct(b_nonadv)}** (threshold: >=70%) — **{_verdict(b_level_ok)}**")
    lines.append(f"- Strategy: {b.strategy}")
    if not b_level_ok:
        lines.append(
            "  - **Finding, not a threshold miscalibration**: Stage 8's diversity re-rank "
            "(`_diversify`, `app/recommendations/service.py`) caps how many results share a "
            "*category*, but has no concept of *level* at all — a beginner-signaled user can "
            "still fill remaining diversity slots with `advanced`-labeled courses from other "
            "categories once their own category's cap is reached. Section 23's expectation "
            "(\"stays beginner/intermediate, explicitly avoiding advanced material\") isn't "
            "fully met by the current ranker. Worth a follow-up: either exclude/penalize "
            "`level='advanced'` candidates when the profile's own engaged levels are entirely "
            "beginner/intermediate, or surface level as a second diversity axis alongside "
            "category. Not fixed here — this is Stage 17's eval framework surfacing a Stage 8 "
            "ranking gap, not a Stage 17 concern to silently patch."
        )
    lines.append("")
    lines.append("## Journey C — pivots mid-journey (data analytics -> backend/system design)")
    lines.append("")
    lines.append(f"- Before-pivot categories: {c_before.categories} (dominant profile category: {c_before.dominant_profile_category})")
    lines.append(f"- After-pivot categories: {c_after.categories} (dominant profile category: {c_after.dominant_profile_category})")
    lines.append(f"- Category-set overlap (Jaccard): **{c_overlap:.2f}** (threshold: <=0.50, lower = more responsive to the pivot) — **{_verdict(c_ok)}**")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Narration grounding rate across all {len(RUNS)} runs: **{_fmt_pct(grounded_rate)}**")
    lines.append(f"- Average generation latency (embeddings via local hash placeholder, no real Mesh network call): **{avg_latency:.1f}ms**")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    all_ok = a_ok and b_cap_ok and b_level_ok and c_ok
    overall = "PASS" if all_ok else "WARN — see individual journeys above"
    lines.append(f"**{overall}**")
    lines.append("")
    lines.append(
        "Grounding/hallucination-rejection correctness is NOT re-evaluated here — see "
        "tests/test_narration.py for that (Section 18's \"single most important test in the "
        "system\": a fabricated citation index gets rejected). This report is specifically "
        "about retrieval/ranking relevance and recency-weighting behavior, which pytest's "
        "exact-input unit tests don't directly measure."
    )
    return "\n".join(lines)


def main() -> None:
    original_provider = embeddings_module._provider
    original_chat = mesh_client.chat
    embeddings_module._provider = embeddings_module.LocalHashEmbeddingProvider()
    mesh_client.chat = _fake_chat

    original_vector_path = settings.vector_db_path
    tmp_vector_dir = tempfile.mkdtemp(prefix="chennai_labs_eval_chroma_")
    settings.vector_db_path = tmp_vector_dir
    vs_module._client = None

    try:
        db = _make_db()
        catalog = _seed_catalog(db)
        now = utcnow()

        a = journey_a(db, catalog, now)
        b = journey_b(db, catalog, now)
        c_before, c_after = journey_c(db, catalog, now)

        report = build_report(a, b, c_before, c_after)
        print(report)
        REPORT_PATH.write_text(report + "\n")
        print(f"\nWrote {REPORT_PATH}")
    finally:
        embeddings_module._provider = original_provider
        mesh_client.chat = original_chat
        settings.vector_db_path = original_vector_path
        vs_module._client = None
        shutil.rmtree(tmp_vector_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
