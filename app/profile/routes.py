"""
GET /profile — the "inspectable" half of Stage 6's requirement. Every
number on this page traces back to a formula in app/profile/service.py
run over this one user's own rows; nothing here is a model output
they have to take on faith. Login-gated by `require_user` like
/dashboard — a profile is meaningless (and would leak nothing, since
it's scoped to `user.id`, but there's no reason to expose it) without
an account behind it.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import require_user
from app.db.models.user import User
from app.db.session import get_db
from app.profile.service import build_interest_profile
from app.templating import templates

router = APIRouter()


@router.get("/profile")
async def profile(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    interest_profile = build_interest_profile(db, user.id)
    return templates.TemplateResponse(
        request,
        "profile/index.html",
        {"user": user, "current_user": user, "profile": interest_profile},
    )
