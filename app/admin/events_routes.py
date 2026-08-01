"""
The event-debug view the Stage 5 brief explicitly asks for: a way to
SEE captured events, not just trust that ingestion is working. Kept as
its own router/file (rather than folded into app/admin/routes.py, which
already owns /admin/products) because it's a genuinely separate concern
— read-only observability, not catalog management — even though both
live under /admin and share `require_admin`.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.db.models.user import User
from app.db.models.user_event import VALID_EVENT_TYPES
from app.db.session import get_db
from app.events import service
from app.templating import templates

router = APIRouter(prefix="/admin/events")


@router.get("")
async def list_events(
    request: Request,
    event_type: str | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    events = service.list_recent_events(db, event_type=event_type, limit=200)
    return templates.TemplateResponse(
        request,
        "admin/events/index.html",
        {
            "events": events,
            "event_types": VALID_EVENT_TYPES,
            "selected_type": event_type or "",
            "total_count": service.count_events(db),
            "current_user": admin,
        },
    )
