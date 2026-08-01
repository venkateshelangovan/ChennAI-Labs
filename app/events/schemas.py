"""
Request-boundary validation for the event ingestion endpoint. This is
the one place a malformed payload gets rejected with structure — once
past here, app/events/service.py deals only in clean data.
"""

from pydantic import BaseModel, Field

from app.db.models.user_event import VALID_EVENT_TYPES

MAX_EVENTS_PER_BATCH = 50  # cheap abuse guard; real rate limiting is Stage 16

# Cookie name shared between app/static/js/tracker.js (which sets it) and
# app/auth/routes.py (which reads it, once, at login/register, to run
# reconciliation). Kept as one named constant rather than a string
# duplicated in two languages so a rename can't silently desync them.
TRACKING_SESSION_COOKIE = "cl_session_id"


class EventIn(BaseModel):
    client_event_id: str = Field(..., max_length=64)
    session_id: str = Field(..., max_length=64)
    event_type: str
    product_id: int | None = None
    metadata: dict = Field(default_factory=dict)


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(..., max_length=MAX_EVENTS_PER_BATCH)


def is_valid_event_type(event_type: str) -> bool:
    return event_type in VALID_EVENT_TYPES
