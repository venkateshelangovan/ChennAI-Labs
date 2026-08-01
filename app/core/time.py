"""
One function: the UTC "now" every timestamp in the system uses.

Why this exists: SQLite (our local-dev DB) stores DateTime columns as
naive values — mixing naive and timezone-aware datetimes in comparisons
raises a TypeError. Rather than remembering to strip tzinfo everywhere
we touch a timestamp, every model default and every comparison calls
this one helper, so the whole codebase is naive-but-always-UTC by
convention. Postgres in production stores these the same way as long as
columns stay untyped-timezone (TIMESTAMP WITHOUT TIME ZONE), which is
what SQLAlchemy's plain DateTime maps to on both engines — so this
convention survives the SQLite-to-Postgres migration without changes.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
