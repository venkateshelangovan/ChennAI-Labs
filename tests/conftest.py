"""
Shared test fixtures.

Every test gets a fresh in-memory SQLite database — `Base.metadata`
already knows about every model because `app.main` (imported here via
`app.auth.dependencies`) pulls in `app.auth.routes` -> `app.auth.service`
-> the model modules as a side effect of import. `StaticPool` is what
makes an in-memory SQLite DB usable across multiple connections/threads
within one test; without it, each new connection would see an empty,
unrelated database.

We override the `get_db` dependency (not `app.db.session.SessionLocal`
directly) because that's the seam the application itself depends on —
overriding it is exactly what production code would see if it were
pointed at a different database, which is the property that makes this
a meaningful test rather than one that mocks around the real code path.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base, register_models
from app.db.session import get_db
from app.retrieval import vector_store as vs_module

register_models()


@pytest.fixture(autouse=True)
def isolated_vector_store(tmp_path, monkeypatch):
    """
    Every product create/update/archive/restore now triggers a vector
    write (Stage 4). Without this fixture, every test in the suite
    would share ONE on-disk Chroma collection while each test gets its
    own fresh, autoincrement-from-1 SQLite database — product #1 in one
    test would silently collide with product #1 in another. Autouse so
    no test has to remember to ask for isolation; it's not opt-in
    because forgetting it would produce flaky, order-dependent failures
    rather than a clean error.
    """
    monkeypatch.setattr(settings, "vector_db_path", str(tmp_path / "chroma"))
    vs_module._client = None
    yield
    vs_module._client = None


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_sessionmaker(db_engine):
    return sessionmaker(bind=db_engine, autoflush=False, autocommit=False)


@pytest.fixture()
def client(db_sessionmaker):
    from app.main import app

    def override_get_db():
        db = db_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def db_session(db_sessionmaker):
    session = db_sessionmaker()
    yield session
    session.close()
