"""
The declarative base every model inherits from, plus a single place
(`base_models`) that guarantees every model module has been imported
before Alembic or `create_all` inspects `Base.metadata` — SQLAlchemy
only knows about a table once its model class has been imported
somewhere.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def register_models() -> None:
    """Import every model module for its side effect of registering on Base.metadata."""
    from app.db.models import user, auth_session, product, user_event, recommendation_snapshot  # noqa: F401
