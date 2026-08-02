"""
Stage 15 (bonus): the proactive digest.

--- What "digest" means here ---

Stage 0 Section 15 lists a daily digest as a bonus feature that
"delivers" fresh recommendations to users. A full document grep found
no email/SMTP/notification infrastructure planned anywhere else in the
spec — no `smtplib`, no email templates, no "From:"/"Subject:" fields,
nothing. So "delivers" is interpreted narrowly and honestly: this job
does NOT send anything to anyone. It regenerates and persists a fresh
`RecommendationSnapshot` for every user who already has one, so the
NEXT time they open their dashboard, Stage 12's cache serves an
already-current recommendation instead of paying the generation
latency in-request. That's the entire scope. Building a real email
pipeline here would be inventing a feature the spec never actually
describes building.

--- Why every existing user, not just "active" ones ---

The trigger already used in `get_dashboard_recommendations`
(app/recommendations/trigger.py) decides per-request whether a
snapshot is stale enough to regenerate. The digest doesn't re-run that
logic — it unconditionally regenerates every user who has a snapshot
row at all, once a day. Two reasons: (1) simplicity — this is a bonus
feature, and re-deriving "would trigger.evaluate() have said yes"
outside of a request context just to decide whether to do the same
work trigger.evaluate() would trigger anyway is circular; (2) it's the
correct behavior for the stated goal — the point of a *proactive*
digest is to pay the generation cost during idle/off-peak hours
instead of when a real user is waiting on a page load, which only
matters if it actually runs the pipeline rather than skipping users
who'd have been fresh anyway.

Users who have never visited a dashboard (no snapshot row yet) are
deliberately excluded — there's nothing to keep fresh for them, and
generating a snapshot they've never asked for would just be unrequested
work with no user-facing benefit until they visit anyway (at which
point Stage 12 generates it in-request as normal).

--- Per-user error isolation ---

One user's retrieval/narration failure (a Mesh timeout, a data
oddity) must not abort the whole run and leave every other user's
snapshot stale. Each user is wrapped in its own try/except; failures
are logged and counted, and the run continues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.db.models.recommendation_snapshot import RecommendationSnapshot
from app.recommendations.cache import regenerate_and_persist

logger = logging.getLogger("chennai_labs.recommendations.digest")

TRIGGER_REASON = "scheduled_digest"


@dataclass
class DigestSummary:
    total_users: int
    succeeded: int = 0
    failed: int = 0
    failed_user_ids: list[int] = field(default_factory=list)


def run_daily_digest(db: Session, *, now: datetime | None = None) -> DigestSummary:
    now = now or utcnow()

    user_ids = [
        row[0]
        for row in db.query(RecommendationSnapshot.user_id).order_by(RecommendationSnapshot.user_id).all()
    ]

    summary = DigestSummary(total_users=len(user_ids))

    logger.info("digest_run_started", extra={"total_users": summary.total_users})

    for user_id in user_ids:
        try:
            regenerate_and_persist(db, user_id, trigger_reason=TRIGGER_REASON, now=now)
            summary.succeeded += 1
        except Exception:
            # Isolated per user on purpose — see module docstring. Roll
            # back so a half-written snapshot from the failed attempt
            # doesn't linger in the session for the next user's query.
            db.rollback()
            summary.failed += 1
            summary.failed_user_ids.append(user_id)
            logger.exception("digest_user_failed", extra={"user_id": user_id})

    logger.info(
        "digest_run_completed",
        extra={
            "total_users": summary.total_users,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
        },
    )

    return summary
