"""
The catalog table. Matches Stage 0's field list, minus two columns
deliberately deferred to Stage 4: `content_hash` and `vector_sync_status`
only mean something once a vector store exists to be in or out of sync
with — adding them now would be dead columns with no code path
exercising them. They arrive as part of Stage 4's migration, alongside
the dual-write logic that gives them meaning.

Why `rating` has no admin input (see app/products/routes.py): it's
meant to be an aggregate of real user reviews, which this system
doesn't collect yet. Exposing it as an admin-editable number would
make it fake data pretending to be a signal — seed data sets it
directly (as a stand-in for "this course has an established track
record"), but the CRUD form does not, so nothing in the live product
path can silently fabricate a rating.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Index, JSON, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.time import utcnow
from app.db.base import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("level IN ('beginner', 'intermediate', 'advanced')", name="ck_products_level_valid"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_products_status_valid"),
        Index("ix_products_category_status", "category", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    category: Mapped[str] = mapped_column(String(80), nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String(80), nullable=True)

    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    instructor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    rating: Mapped[float | None] = mapped_column(nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def duration_hours(self) -> float:
        return round(self.duration_minutes / 60, 1)
