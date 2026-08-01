"""
All auth business logic lives here — registration, authentication,
session issuance/lookup/revocation. Routes (app/auth/routes.py) only
handle HTTP concerns (parsing the form, rendering a template, setting a
cookie); everything that touches the database or makes a security
decision is a function here, which is what makes this layer testable
without spinning up HTTP at all (see tests/test_auth.py's direct calls
into this module).
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_session_token, hash_password, verify_password
from app.core.time import utcnow
from app.db.models.auth_session import AuthSession
from app.db.models.user import User


class EmailAlreadyRegistered(Exception):
    pass


class InvalidCredentials(Exception):
    pass


def register_user(
    db: Session, *, email: str, password: str, display_name: str, role: str = "user"
) -> User:
    normalized_email = email.strip().lower()
    if db.query(User).filter(User.email == normalized_email).first() is not None:
        raise EmailAlreadyRegistered(normalized_email)

    user = User(
        email=normalized_email,
        password_hash=hash_password(password),
        display_name=display_name.strip(),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User:
    normalized_email = email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    # Deliberately identical error for "no such user" and "wrong password" —
    # distinguishing them lets an attacker enumerate valid emails.
    if user is None or not verify_password(password, user.password_hash):
        raise InvalidCredentials()
    return user


def create_session(db: Session, user: User) -> AuthSession:
    session = AuthSession(
        token=generate_session_token(),
        user_id=user.id,
        expires_at=utcnow() + timedelta(days=settings.session_ttl_days),
    )
    db.add(session)
    db.commit()
    return session


def get_user_by_session_token(db: Session, token: str | None) -> User | None:
    if not token:
        return None

    session = db.query(AuthSession).filter(AuthSession.token == token).first()
    if session is None:
        return None

    if session.expires_at < utcnow():
        db.delete(session)
        db.commit()
        return None

    return db.query(User).filter(User.id == session.user_id).first()


def delete_session(db: Session, token: str | None) -> None:
    if not token:
        return
    db.query(AuthSession).filter(AuthSession.token == token).delete()
    db.commit()
