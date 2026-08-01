"""
FastAPI dependencies for auth. This is the ONLY place role checks
happen — a route that needs a logged-in user declares
`user: User = Depends(require_user)`, a route that needs an admin
declares `user: User = Depends(require_admin)`, and that's the entire
authorization check. No route body ever contains `if user.role != ...`;
scattering that check across routes is exactly how you eventually miss
one.
"""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import service
from app.core.exceptions import NotAuthenticated, NotAuthorized
from app.db.models.user import User
from app.db.session import get_db

SESSION_COOKIE_NAME = "session_token"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return service.get_user_by_session_token(db, token)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise NotAuthenticated()
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise NotAuthorized()
    return user
