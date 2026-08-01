"""
All catalog business logic. Same split as auth (Stage 2): routes handle
HTTP, this module handles the database and the decisions — slug
generation/uniqueness, search matching, what "active" means for public
listings. Nothing here is AI-adjacent; this is the deterministic
catalog layer Stage 7+ will build retrieval on top of, not a
replacement for it.

Search here is intentionally simple: a case-insensitive substring match
against title, description, and tags via SQL LIKE. This is NOT the
semantic retrieval the recommendation engine will use starting Stage 7
— it's "let a user type 'rag' and find the RAG course," which a vector
index would be overkill for. Keeping this distinction explicit now
avoids confusing "catalog search" with "personalized retrieval" later.

Stage 4 addition: create/update/archive/restore each call into
app/retrieval/sync.py AFTER their own db.commit() succeeds — SQL is
always the write of record; the vector write is a best-effort follow-up
that can fail without the product write itself failing (Stage 0,
Section 9). This is "dual write," not "distributed transaction": there
is no rollback of the SQL side if the vector side fails.
"""

import re
from decimal import Decimal

from sqlalchemy import String, or_
from sqlalchemy.orm import Session

from app.db.models.product import Product
from app.retrieval import sync as vector_sync

VALID_LEVELS = ("beginner", "intermediate", "advanced")


class ProductNotFound(Exception):
    pass


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "course"


def generate_unique_slug(db: Session, title: str, *, exclude_id: int | None = None) -> str:
    base = slugify(title)
    candidate = base
    suffix = 2
    while True:
        query = db.query(Product).filter(Product.slug == candidate)
        if exclude_id is not None:
            query = query.filter(Product.id != exclude_id)
        if query.first() is None:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def list_products(
    db: Session,
    *,
    category: str | None = None,
    level: str | None = None,
    q: str | None = None,
    include_archived: bool = False,
) -> list[Product]:
    query = db.query(Product)
    if not include_archived:
        query = query.filter(Product.status == "active")
    if category:
        query = query.filter(Product.category == category)
    if level:
        query = query.filter(Product.level == level)
    if q:
        needle = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Product.title.ilike(needle),
                Product.description.ilike(needle),
                # SQLite/Postgres both store `tags` as JSON text under the hood via
                # SQLAlchemy's JSON type, so a LIKE against its string form is a
                # pragmatic way to match tags without a dedicated tags table —
                # a real inverted index isn't warranted for a few hundred rows.
                Product.tags.cast(String).ilike(needle),
            )
        )
    return query.order_by(Product.created_at.desc()).all()


def list_categories(db: Session) -> list[str]:
    rows = db.query(Product.category).filter(Product.status == "active").distinct().order_by(Product.category).all()
    return [row[0] for row in rows]


def get_product_by_slug(db: Session, slug: str, *, include_archived: bool = False) -> Product:
    query = db.query(Product).filter(Product.slug == slug)
    if not include_archived:
        query = query.filter(Product.status == "active")
    product = query.first()
    if product is None:
        raise ProductNotFound(slug)
    return product


def get_product_by_id(db: Session, product_id: int) -> Product:
    product = db.query(Product).filter(Product.id == product_id).first()
    if product is None:
        raise ProductNotFound(product_id)
    return product


def create_product(
    db: Session,
    *,
    title: str,
    description: str,
    category: str,
    subcategory: str | None,
    price: Decimal,
    level: str,
    tags: list[str],
    instructor: str | None,
    duration_minutes: int,
    image_url: str | None,
    rating: float | None = None,
) -> Product:
    product = Product(
        slug=generate_unique_slug(db, title),
        title=title.strip(),
        description=description.strip(),
        category=category.strip(),
        subcategory=(subcategory or "").strip() or None,
        price=price,
        level=level,
        tags=tags,
        instructor=(instructor or "").strip() or None,
        duration_minutes=duration_minutes,
        image_url=(image_url or "").strip() or None,
        rating=rating,
        status="active",
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    vector_sync.sync_product(db, product)  # SQL write already committed; this can fail independently
    return product


def update_product(
    db: Session,
    product: Product,
    *,
    title: str,
    description: str,
    category: str,
    subcategory: str | None,
    price: Decimal,
    level: str,
    tags: list[str],
    instructor: str | None,
    duration_minutes: int,
    image_url: str | None,
) -> Product:
    # Slug is regenerated from the title only if the title actually
    # changed — otherwise editing unrelated fields would silently break
    # every existing link/bookmark to this product's URL.
    if title.strip() != product.title:
        product.slug = generate_unique_slug(db, title, exclude_id=product.id)

    product.title = title.strip()
    product.description = description.strip()
    product.category = category.strip()
    product.subcategory = (subcategory or "").strip() or None
    product.price = price
    product.level = level
    product.tags = tags
    product.instructor = (instructor or "").strip() or None
    product.duration_minutes = duration_minutes
    product.image_url = (image_url or "").strip() or None
    db.commit()
    db.refresh(product)
    # sync_product() itself decides whether this actually needs re-embedding
    # (content_hash comparison) — editing only the price still calls this,
    # but it's a no-op AI-cost-wise if the embedded text didn't change.
    vector_sync.sync_product(db, product)
    return product


def archive_product(db: Session, product: Product) -> Product:
    product.status = "archived"
    db.commit()
    db.refresh(product)
    vector_sync.remove_product(db, product)  # archived products must not be retrievable
    return product


def restore_product(db: Session, product: Product) -> Product:
    product.status = "active"
    db.commit()
    db.refresh(product)
    vector_sync.sync_product(db, product)  # re-embed unconditionally — content_hash was cleared on archive
    return product


def parse_tags(raw: str) -> list[str]:
    """Turn a comma-separated admin-form field into a clean tag list."""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]
