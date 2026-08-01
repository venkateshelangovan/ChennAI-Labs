"""
The catalog table. Matches Stage 0's field list. `content_hash` and
`vector_sync_status` were deliberately deferred out of the Stage 3
migration — they only mean something once a vector store exists to be
in or out of sync with. They arrive now, in Stage 4, alongside the
dual-write logic that gives them meaning:

- `content_hash`: a hash of the exact text that was embedded (see
  app/retrieval/embeddings.py's build_embedding_text). Comparing this
  against a freshly computed hash on update is how we decide whether a
  re-embed is actually necessary — editing the price shouldn't trigger
  a new embedding call.
- `vector_sync_status`: 'pending' | 'synced' | 'failed'. This is the
  mechanism from Stage 0 Section 9 — SQL is always the write that must
  succeed; the vector write is attempted after, and if it fails, this
  column is what makes that visible and repairable instead of silent.

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
        CheckConstraint(
            "vector_sync_status IN ('pending', 'synced', 'failed')", name="ck_products_vector_sync_status_valid"
        ),
        Index("ix_products_category_status", "category", "status"),
        Index("ix_products_vector_sync_status", "vector_sync_status"),
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

    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_sync_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def duration_hours(self) -> float:
        return round(self.duration_minutes / 60, 1)
