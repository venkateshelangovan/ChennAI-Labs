"""
Turns the raw, append-only user_events table into a deterministic
"interest profile" — the input Stage 7's retrieval and Stage 8's
recommendation pipeline will read.

Deliberately NOT AI: no embeddings, no LLM call, nothing that isn't
reproducible by re-running the same arithmetic on the same rows. Mesh
doesn't exist in this codebase until Stage 9, and this would be exactly
the wrong place to reach for it early even if it did — a user's own
browsing history is the one recommendation input that should be fully
inspectable and explainable (Stage 0's "no black box" goal), and a
hand-rolled recency-weighted sum is auditable in a way a model call
never is. Semantic similarity over course content is Stage 7's job,
once there's a real embedding provider to compute it with.

--- The algorithm ---

Every qualifying event contributes a weight to one or more targets (a
category, a tag, a product):

    weight = base_weight(event) * recency_decay(event, now)

`recency_decay` is an exponential half-life: a signal's contribution
halves every HALF_LIFE_DAYS and asymptotically approaches, but never
hits, zero. Interests fade — a course someone was obsessed with three
months ago should count for less than one they viewed yesterday — but
nothing is ever hard-deleted from the profile the way a fixed lookback
window (e.g. "only the last 30 days") would. That matters most for
someone who was active, went quiet, and comes back weeks later: a hard
window would show them a blank profile despite a real history sitting
right there in user_events; decay just means that history speaks more
softly.

`base_weight` differs by event type because they are not equally
strong signals of interest:
- click (2.0): an explicit choice to open something from a list —
  stronger than merely landing on a page.
- category_view (1.5): browsing into a whole category is a coarser
  but still deliberate signal, so it gets its own middle weight.
- view (1.0): the baseline unit every other weight is defined relative
  to — you get here either by clicking through or a direct link/reload.
- time_spent: not a flat weight at all, since dwell time is a
  continuous quantity, not a discrete choice — see
  `_time_spent_weight`.
- search: contributes NO category or tag weight. A free-text query
  cannot be reliably mapped to a category by keyword-matching without
  exactly the kind of semantic understanding this stage is explicitly
  avoiding — "roc curve precision recall" belongs in "Data Analyst /
  Data Scientist," but nothing short of real semantic search (Stage 7)
  can know that safely. Rather than fake a mapping, search queries are
  preserved verbatim (`search_terms`) so Stage 7 can use the raw text
  once it exists to use it correctly.

Category and tag weights are each normalized (divided by their own
total) so a profile reads as "relative share of attention" — numbers
that mean something on their own, not raw sums that only make sense
compared side by side. Product weights are instead normalized against
this user's single strongest product signal, because "how does this
compare to your other product signals" (a bar chart headed by 100%) is
the more useful reading for the direct "top products" list this feeds.

No pagination on the underlying event query yet — every event this
user has ever generated is read and summed on every call. Fine at this
project's scale; if that ever changed, the fix would follow the same
"try/failed/reconcile" spirit as Stage 4's dual-write and Stage 12's
caching, not a redesign of the math itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.product import Product
from app.db.models.user_event import UserEvent
from app.profile.schemas import CategoryScore, InterestProfile, ProductAffinity, SearchTerm, TagScore

HALF_LIFE_DAYS = 14.0

# time_spent isn't a flat weight — it scales with dwell time, but is
# capped so someone leaving a tab open overnight doesn't dominate every
# other signal in the profile. 240s of dwell = the maximum weight
# (8.0 units); 30s of dwell = 1.0 unit, i.e. roughly "as strong as a view".
TIME_SPENT_CAP_MS = 240_000
TIME_SPENT_UNIT_MS = 30_000

BASE_WEIGHTS = {
    "click": 2.0,
    "category_view": 1.5,
    "view": 1.0,
}

TOP_PRODUCTS_LIMIT = 10
TOP_TAGS_LIMIT = 15
SEARCH_TERMS_LIMIT = 15


def _recency_decay(created_at: datetime, now: datetime) -> float:
    age_days = (now - created_at).total_seconds() / 86400
    age_days = max(age_days, 0.0)  # clock skew / same-instant events never count as "future"
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _time_spent_weight(event_metadata: dict) -> float:
    duration_ms = (event_metadata or {}).get("duration_ms") or 0
    try:
        duration_ms = float(duration_ms)
    except (TypeError, ValueError):
        duration_ms = 0.0
    duration_ms = min(max(duration_ms, 0.0), TIME_SPENT_CAP_MS)
    return duration_ms / TIME_SPENT_UNIT_MS


def _normalize_categories(weights: dict[str, float], evidence: dict[str, int]) -> list[CategoryScore]:
    total = sum(weights.values())
    if total <= 0:
        return []
    scored = [
        CategoryScore(category=key, score=round(w / total, 4), evidence_count=evidence[key])
        for key, w in weights.items()
    ]
    return sorted(scored, key=lambda c: c.score, reverse=True)


def _normalize_tags(weights: dict[str, float], evidence: dict[str, int]) -> list[TagScore]:
    total = sum(weights.values())
    if total <= 0:
        return []
    scored = [
        TagScore(tag=key, score=round(w / total, 4), evidence_count=evidence[key])
        for key, w in weights.items()
    ]
    return sorted(scored, key=lambda t: t.score, reverse=True)


def build_interest_profile(db: Session, user_id: int, *, now: datetime | None = None) -> InterestProfile:
    now = now or utcnow()

    # Newest-first: matters for search_terms below, where the first time
    # we see a given query text should be its most recent occurrence.
    events = (
        db.query(UserEvent)
        .filter(UserEvent.user_id == user_id)
        .order_by(UserEvent.created_at.desc())
        .all()
    )

    if not events:
        return InterestProfile(user_id=user_id, generated_at=now, sample_size=0)

    product_ids = {e.product_id for e in events if e.product_id is not None}
    products: dict[int, Product] = {}
    if product_ids:
        products = {p.id: p for p in db.query(Product).filter(Product.id.in_(product_ids)).all()}

    category_weight: dict[str, float] = {}
    category_evidence: dict[str, int] = {}
    tag_weight: dict[str, float] = {}
    tag_evidence: dict[str, int] = {}
    product_weight: dict[int, float] = {}
    product_evidence: dict[int, int] = {}
    search_terms: dict[str, SearchTerm] = {}

    for event in events:
        decay = _recency_decay(event.created_at, now)

        if event.event_type == "search":
            query = (event.event_metadata or {}).get("query")
            query = query.strip() if isinstance(query, str) else None
            if not query:
                continue
            key = query.lower()
            if key in search_terms:
                search_terms[key].count += 1
            else:
                # events are iterated newest-first, so the first time we
                # see this key IS the most recent occurrence
                search_terms[key] = SearchTerm(query=query, count=1, last_searched_at=event.created_at)
            continue

        if event.event_type == "category_view":
            category = (event.event_metadata or {}).get("category")
            if not category:
                continue
            weight = BASE_WEIGHTS["category_view"] * decay
            category_weight[category] = category_weight.get(category, 0.0) + weight
            category_evidence[category] = category_evidence.get(category, 0) + 1
            continue

        # view / click / time_spent are all product-anchored — enforced
        # at ingestion (PRODUCT_REQUIRED_TYPES in app/events/service.py),
        # so product_id should always be set here, but we look the
        # product up defensively rather than assume the join succeeds.
        product = products.get(event.product_id) if event.product_id else None
        if product is None:
            continue

        if event.event_type == "time_spent":
            weight = _time_spent_weight(event.event_metadata) * decay
        else:
            weight = BASE_WEIGHTS.get(event.event_type, 0.0) * decay

        if weight <= 0:
            continue

        category_weight[product.category] = category_weight.get(product.category, 0.0) + weight
        category_evidence[product.category] = category_evidence.get(product.category, 0) + 1

        product_weight[product.id] = product_weight.get(product.id, 0.0) + weight
        product_evidence[product.id] = product_evidence.get(product.id, 0) + 1

        if product.tags:
            share = weight / len(product.tags)
            for tag in product.tags:
                tag_weight[tag] = tag_weight.get(tag, 0.0) + share
                tag_evidence[tag] = tag_evidence.get(tag, 0) + 1

    top_products: list[ProductAffinity] = []
    if product_weight:
        max_weight = max(product_weight.values())
        ranked = sorted(product_weight.items(), key=lambda kv: kv[1], reverse=True)[:TOP_PRODUCTS_LIMIT]
        for product_id, weight in ranked:
            product = products[product_id]
            top_products.append(
                ProductAffinity(
                    product_id=product_id,
                    slug=product.slug,
                    title=product.title,
                    category=product.category,
                    relative_score=round(weight / max_weight, 4) if max_weight else 0.0,
                    evidence_count=product_evidence[product_id],
                )
            )

    ranked_search_terms = sorted(search_terms.values(), key=lambda s: s.last_searched_at, reverse=True)[
        :SEARCH_TERMS_LIMIT
    ]

    return InterestProfile(
        user_id=user_id,
        generated_at=now,
        sample_size=len(events),
        category_scores=_normalize_categories(category_weight, category_evidence),
        tag_scores=_normalize_tags(tag_weight, tag_evidence)[:TOP_TAGS_LIMIT],
        top_products=top_products,
        search_terms=ranked_search_terms,
    )
