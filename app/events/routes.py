"""
The single ingestion endpoint app/static/js/tracker.js talks to.

No CSRF token here — deliberately. CSRF protection exists to stop a
malicious site from performing consequential actions using a logged-in
user's cookie (changing a password, placing an order). This endpoint
only ever writes analytics rows, and our session cookie is
`samesite=lax`, which already blocks it being attached to a cross-site
POST — so a forged request from another origin lands as an anonymous
event with no user_id, not an authenticated one. Requiring a CSRF token
would also be awkward for `navigator.sendBeacon`, which can't easily
carry one. This is a deliberate, scoped exception, not an oversight.

No auth requirement either — the whole point of this endpoint is to
capture behavior from visitors who haven't registered yet.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.session import get_db
from app.events import service
from app.events.schemas import EventBatchIn

logger = logging.getLogger("chennai_labs.events")

router = APIRouter()


@router.post("/api/events")
async def ingest(batch: EventBatchIn, request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    current_user = get_current_user(request, db)
    result = service.ingest_events(db, batch.events, user_id=current_user.id if current_user else None)
    logger.info("events_ingested", extra=result.as_dict())
    return JSONResponse(result.as_dict())
