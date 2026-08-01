"""
Server-side session storage backing cookie auth. The token itself is the
primary key — a lookup is just `SELECT ... WHERE token = ?` — and
`ON DELETE CASCADE` means deleting a user (support/admin action, or a
future account-deletion feature) can never leave orphaned sessions
behind. Logout is a single row delete, not a token-invalidation scheme,
which is the whole reason we chose opaque server-side sessions over a
self-contained JWT (Stage 0, Section 16).
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class AuthSession(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
