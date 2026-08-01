"""
The identity table. Matches the ER design from Stage 0, Section 7.

`role` is a plain string constrained to "user"/"admin" at the
application layer (see CheckConstraint below) rather than a separate
`roles` table — a join table buys us nothing with exactly two roles and
no near-term plan for more; adding it later, if roles ever become
data-driven, doesn't require touching anything that references
`User.role` today since the attribute name wouldn't change.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('user', 'admin')", name="ck_users_role_valid"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
