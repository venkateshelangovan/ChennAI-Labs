"""
Value objects for the interest profile. Unlike app/events/schemas.py
(which validates untrusted JSON arriving from the client), nothing here
parses external input — these are just typed containers for what
app/profile/service.py computes, so app/profile/routes.py and its
template have a stable shape to render instead of passing raw dicts
around. Plain dataclasses rather than Pydantic models, on purpose:
there's no request/response boundary here to validate against.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CategoryScore:
    category: str
    score: float  # normalized share of attention across all categories, 0..1, sums to 1
    evidence_count: int  # how many raw events contributed to this category


@dataclass
class TagScore:
    tag: str
    score: float  # normalized share of attention across all tags, 0..1, sums to 1
    evidence_count: int


@dataclass
class ProductAffinity:
    product_id: int
    slug: str
    title: str
    category: str
    relative_score: float  # 0..1, relative to this user's single strongest product signal
    evidence_count: int


@dataclass
class SearchTerm:
    query: str
    count: int
    last_searched_at: datetime


@dataclass
class InterestProfile:
    user_id: int
    generated_at: datetime
    sample_size: int  # total events (of every type) this profile was built from
    category_scores: list[CategoryScore] = field(default_factory=list)
    tag_scores: list[TagScore] = field(default_factory=list)
    top_products: list[ProductAffinity] = field(default_factory=list)
    search_terms: list[SearchTerm] = field(default_factory=list)

    @property
    def is_cold_start(self) -> bool:
        """
        No behavioral signal yet. The UI shows an explicit "browse a bit
        first" message instead of an empty chart, and Stages 7/8 fall
        back to non-personalized ranking rather than dividing by a zero
        total — the same "cold start" case every recsys has to handle.
        """
        return self.sample_size == 0

    def as_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "generated_at": self.generated_at.isoformat(),
            "sample_size": self.sample_size,
            "is_cold_start": self.is_cold_start,
            "category_scores": [c.__dict__ for c in self.category_scores],
            "tag_scores": [t.__dict__ for t in self.tag_scores],
            "top_products": [p.__dict__ for p in self.top_products],
            "search_terms": [
                {**s.__dict__, "last_searched_at": s.last_searched_at.isoformat()} for s in self.search_terms
            ],
        }
