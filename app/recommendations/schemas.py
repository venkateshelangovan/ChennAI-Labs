"""
Value objects for the recommendation pipeline.

Unlike Stage 6/7's InterestProfile and RetrievalResult (which describe
data that doesn't exist anywhere else — a computed profile, a ranked
candidate list), a Recommendation wraps the real `Product` ORM row
rather than duplicating its display fields (title, price, description,
duration, rating, ...). This list feeds directly into the existing
`partials/product_card.html` macro from Stage 3 — duplicating every
field that macro reads would just be a second place for them to drift
out of sync with the actual catalog. `reason` and `similarity` are the
only genuinely new information a Recommendation adds on top of the
product itself.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.db.models.product import Product


@dataclass
class Recommendation:
    product: Product
    reason: str  # one of a small fixed set of deterministic templates — see service.py
    similarity: float | None  # None for the popularity fallback path, which has no query to compare against


@dataclass
class RecommendationResult:
    user_id: int
    generated_at: datetime
    strategy: str  # "personalized" | "popular_fallback"
    recommendations: list[Recommendation] = field(default_factory=list)
    retrieval_refined: bool = False  # Stage 11: did the quality-gate loop have to narrow the query and retry?
    # Stage 14: "why did this user get this recommendation," answerable
    # from the row alone (Section 17) — retrieval attempts, the raw
    # candidate pool with scores, and which product IDs were excluded
    # for already being engaged with. Persisted verbatim into
    # RecommendationSnapshot.trace by app/recommendations/cache.py;
    # never read by anything user-facing. See service.py for exactly
    # what populates this per code path.
    trace: dict = field(default_factory=dict)

    @property
    def is_personalized(self) -> bool:
        return self.strategy == "personalized"
