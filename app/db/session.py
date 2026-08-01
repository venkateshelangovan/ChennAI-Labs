"""
The SQLAlchemy engine and session factory. `SessionLocal()` gives you a
new `Session`; `get_db()` is the FastAPI dependency every route/service
call uses to get one that's automatically closed after the request.

`connect_args={"check_same_thread": False}` is SQLite-specific — SQLite
by default forbids using a connection from a different thread than the
one that created it, but FastAPI can service a request on any worker
thread. This flag is a no-op (and unnecessary) on Postgres, so it's
applied conditionally rather than unconditionally, which is what keeps
this file safe to point at either database without edits.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
