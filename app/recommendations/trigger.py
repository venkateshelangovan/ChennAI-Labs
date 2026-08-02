"""
Stage 12: the deterministic "should we regenerate this user's
recommendation right now" decision — Stage 0's Section 14 table exists
specifically so this question never costs an AI call to answer. Every
condition here is a DB query against timestamps and event rows, nothing
else.

Before this module existed, every `/dashboard` view called
`generate_recommendations` (Stages 7-11's full retrieval pipeline) AND
`generate_narration` (Stage 10's Mesh chat call) from scratch — correct
individually, but proportional to page views, not to actual behavior
change. A user who reloads their dashboard five times in a minute
hasn't generated five times as much genuine signal; the fifth reload
should be free.

--- The three trigger conditions (Stage 0, Section 14) ---

1. NO SNAPSHOT YET — the user has never had a recommendation computed.
   Always regenerate; there's nothing to serve from cache.
2. TTL ELAPSED *AND* MEANINGFUL SIGNAL — the existing snapshot is older
   than `TTL_HOURS`, *and* at least `MIN_EVENTS_SINCE_REFRESH` events
   (or even one `search` event, a strong explicit-intent signal) have
   landed since it was generated. Both conditions, not either — a
   snapshot that's merely old but the user hasn't done anything new
   isn't stale in any way that matters; regenerating it would spend a
   Mesh call to produce the same answer.
3. MANUAL REFRESH — the user explicitly asks (a "Refresh
   recommendations" action on /dashboard). Section 16 asks for this
   endpoint to be rate-limited "to blunt cost abuse," but Stage 16 (real
   rate limiting) doesn't exist yet. Rather than build ahead of that
   stage, this reuses state that's already here: a manual refresh
   within `MANUAL_REFRESH_COOLDOWN_SECONDS` of the last generation
   (regardless of why that one happened) is refused. The snapshot's own
   `generated_at` doubles as the cooldown marker — no new
   infrastructure, no second source of truth to keep in sync.

Everything not listed above is a cache hit, full stop — including a
merely-stale-but-signal-free snapshot. Being cautious about
regenerating too eagerly is the entire point of this module; a false
"still fresh" is a mildly outdated recommendation, a false "stale"
converts directly into an unnecessary AI spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.db.models.user_event import UserEvent

TTL_HOURS = 24
MIN_EVENTS_SINCE_REFRESH = 5
MANUAL_REFRESH_COOLDOWN_SECONDS = 60


@dataclass
class TriggerDecision:
    should_refresh: bool
    reason: str
    # "no_snapshot" | "ttl_and_signal" | "manual_refresh" |
    # "fresh" | "stale_but_no_signal" | "manual_refresh_on_cooldown"


def _event_count_since(db: Session, user_id: int, since: datetime) -> int:
    return (
        db.query(UserEvent)
        .filter(UserEvent.user_id == user_id, UserEvent.created_at > since)
        .count()
    )


def _has_search_event_since(db: Session, user_id: int, since: datetime) -> bool:
    return db.query(
        exists().where(
            UserEvent.user_id == user_id,
            UserEvent.created_at > since,
            UserEvent.event_type == "search",
        )
    ).scalar()


def evaluate(
    db: Session,
    snapshot: RecommendationSnapshot | None,
    user_id: int,
    *,
    manual: bool = False,
    now: datetime | None = None,
) -> TriggerDecision:
    now = now or utcnow()

    if snapshot is None:
        return TriggerDecision(True, "no_snapshot")

    if manual:
        elapsed = (now - snapshot.generated_at).total_seconds()
        if elapsed < MANUAL_REFRESH_COOLDOWN_SECONDS:
            return TriggerDecision(False, "manual_refresh_on_cooldown")
        return TriggerDecision(True, "manual_refresh")

    age = now - snapshot.generated_at
    if age < timedelta(hours=TTL_HOURS):
        return TriggerDecision(False, "fresh")

    event_count = _event_count_since(db, user_id, snapshot.generated_at)
    has_search = _has_search_event_since(db, user_id, snapshot.generated_at)
    if event_count >= MIN_EVENTS_SINCE_REFRESH or has_search:
        return TriggerDecision(True, "ttl_and_signal")

    return TriggerDecision(False, "stale_but_no_signal")
