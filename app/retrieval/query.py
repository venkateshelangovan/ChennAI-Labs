"""
Stage 7: turning a user's interest profile (Stage 6) into a ranked list
of candidate products via the vector store.

STAGE 7 vs STAGE 9 — read this before touching this file, same caveat
as app/retrieval/embeddings.py's module docstring. The embeddings this
queries against are still `LocalHashEmbeddingProvider`'s non-semantic
bag-of-words vectors. "Retrieval" here means the real plumbing —
profile -> query representation -> embedding -> vector search -> ranked
candidates — built and tested end-to-end against a placeholder. Stage
9 swapping `get_embedding_provider()` for a Mesh-backed provider is
what makes the *results* semantically meaningful; it changes nothing
in this file, which is exactly the point of building the interface
this way back in Stage 4.

This is also explicitly NOT the recommendation pipeline (Stage 8) —
there's no ranking beyond raw vector distance, no diversity/novelty
logic, no business rules, no filtering for "already purchased." It's
the retrieval step in isolation, inspectable on its own at /profile so
each layer can be verified independently instead of debugging one
opaque "recommendations" black box later.

--- Building a query from a profile ---

`LocalHashEmbeddingProvider` is bag-of-words: a token's contribution to
the resulting vector scales with how many times it appears in the
input text (see app/retrieval/embeddings.py). It has no API for "embed
this weighted list of terms" — only `embed(text: str)`. So
`_profile_to_query_text` repeats each category/tag token a number of
times proportional to its normalized profile score, which is the
closest a frequency-counting embedder can get to "respect these
weights" without changing its interface. This repetition trick is a
property of today's *placeholder* provider, not a retrieval design
decision — Mesh (Stage 9) will accept one natural-language description
of the user's interests, no repetition needed. Only this function's
implementation changes then; `retrieve_for_profile`'s signature and
everything downstream of it does not.

--- Distance vs similarity ---

Chroma's default index metric is L2 (squared Euclidean). Both product
and query embeddings are L2-normalized unit vectors (the last step of
`LocalHashEmbeddingProvider.embed`), and for unit vectors
`distance = 2 - 2*cos_similarity`, so `similarity = 1 - distance/2` is
an exact, not approximate, conversion — not a guess. That identity
holds only because both embeddings are unit vectors; if Stage 9's Mesh
provider ever returns unnormalized vectors, this conversion is the one
thing in this file that would need revisiting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.product import Product
from app.profile.schemas import InterestProfile
from app.profile.service import build_interest_profile
from app.retrieval import vector_store
from app.retrieval.embeddings import get_embedding_provider

DEFAULT_TOP_K = 10

# A category/tag at 100% of a user's normalized score repeats this many
# times in the synthesized query text. Categories get a larger scale
# than tags because there are usually few categories carrying most of
# the weight but many tags splitting it thinly — without a larger
# scale, category signal would be drowned out by tag token diversity.
CATEGORY_REPEAT_SCALE = 20
TAG_REPEAT_SCALE = 10


@dataclass
class RetrievalCandidate:
    product_id: int
    slug: str
    title: str
    category: str
    distance: float  # raw L2 distance from the vector store; smaller = closer
    similarity: float  # 1 - distance/2 — see module docstring for why this conversion is exact here


@dataclass
class RetrievalResult:
    user_id: int
    generated_at: datetime
    query_text: str
    is_cold_start: bool  # no profile signal, or no usable tokens to search with
    candidates: list[RetrievalCandidate] = field(default_factory=list)


def _profile_to_query_text(profile: InterestProfile) -> str:
    parts: list[str] = []

    for c in profile.category_scores:
        repeats = max(1, round(c.score * CATEGORY_REPEAT_SCALE))
        parts.extend([c.category] * repeats)

    for t in profile.tag_scores:
        repeats = max(1, round(t.score * TAG_REPEAT_SCALE))
        parts.extend([t.tag] * repeats)

    for p in profile.top_products:
        parts.append(p.title)

    for s in profile.search_terms:
        parts.append(s.query)

    return " ".join(parts)


def retrieve_for_profile(
    db: Session, profile: InterestProfile, *, top_k: int = DEFAULT_TOP_K
) -> RetrievalResult:
    """
    The core function — takes an already-built profile so callers that
    need both the profile itself and its retrieval preview (namely
    GET /profile) don't pay for `build_interest_profile` twice.
    """
    if profile.is_cold_start:
        return RetrievalResult(
            user_id=profile.user_id, generated_at=profile.generated_at, query_text="", is_cold_start=True
        )

    query_text = _profile_to_query_text(profile)
    if not query_text.strip():
        # Sample size > 0 but nothing usable came out of it — e.g. the
        # only events were searches with blank/whitespace queries,
        # which build_interest_profile already discards. Embedding an
        # empty string would just return the zero vector and rank
        # products by an arbitrary tie-break, not a real signal, so we
        # treat this the same as cold start rather than pretend it's a
        # query.
        return RetrievalResult(
            user_id=profile.user_id, generated_at=profile.generated_at, query_text="", is_cold_start=True
        )

    embedding = get_embedding_provider().embed(query_text)
    # `where={"status": "active"}` is defense in depth, not the primary
    # guarantee — archived products are already deleted outright from
    # the index (see app/retrieval/sync.py's remove_product) — but a
    # metadata filter here means a bug in that deletion path can never
    # surface an archived course as a candidate.
    raw_hits = vector_store.query(embedding=embedding, top_k=top_k, where={"status": "active"})

    candidates: list[RetrievalCandidate] = []
    if raw_hits:
        product_ids = [hit["id"] for hit in raw_hits]
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}
        for hit in raw_hits:
            product = products.get(hit["id"])
            if product is None:
                # Vector index references a product SQL doesn't have —
                # shouldn't happen (see detect_drift in sync.py), but a
                # candidate with no backing row is worse than one fewer
                # candidate.
                continue
            distance = hit["distance"]
            candidates.append(
                RetrievalCandidate(
                    product_id=product.id,
                    slug=product.slug,
                    title=product.title,
                    category=product.category,
                    distance=round(distance, 4),
                    similarity=round(max(0.0, min(1.0, 1 - distance / 2)), 4),
                )
            )

    return RetrievalResult(
        user_id=profile.user_id,
        generated_at=profile.generated_at,
        query_text=query_text,
        is_cold_start=False,
        candidates=candidates,
    )


def retrieve_for_user(
    db: Session, user_id: int, *, top_k: int = DEFAULT_TOP_K, now: datetime | None = None
) -> RetrievalResult:
    """Convenience wrapper for callers (tests, future stages) that only have a user_id."""
    now = now or utcnow()
    profile = build_interest_profile(db, user_id, now=now)
    return retrieve_for_profile(db, profile, top_k=top_k)
