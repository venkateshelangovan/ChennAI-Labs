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
from app.retrieval import embeddings as embeddings_module
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


@pytest.fixture(autouse=True)
def isolated_embedding_provider():
    """
    Stage 9 made MeshEmbeddingProvider the default get_embedding_provider()
    return value — a real outbound HTTP call. Without this fixture, every
    test that creates or updates a product (the majority of the suite,
    across nearly every test file) would attempt a real network call to
    Mesh on every run. Force the deterministic local placeholder back as
    the active provider for the duration of each test, the same way
    isolated_vector_store keeps Chroma test-local: swap the module-level
    singleton (`_provider`), not the function that returns it, so
    `get_embedding_provider()` itself is still the one seam Mesh-specific
    tests (tests/test_mesh_client.py, tests/test_embeddings.py) exercise
    directly.

    A plain assignment + manual restore, not monkeypatch.setattr: a few
    existing tests (tests/test_retrieval.py's failure-simulation tests)
    call `monkeypatch.undo()` explicitly mid-test to end a simulated
    outage, which undoes EVERY patch made so far via that test's shared
    monkeypatch fixture instance — including one this fixture made, if it
    used monkeypatch, well before that test intended to touch it. Direct
    assignment isn't tracked by monkeypatch, so it's immune to a test's
    own unrelated `monkeypatch.undo()` call.
    """
    original = embeddings_module._provider
    embeddings_module._provider = embeddings_module.LocalHashEmbeddingProvider()
    yield
    embeddings_module._provider = original


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
