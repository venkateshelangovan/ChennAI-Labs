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

from app.core import rate_limit
from app.core.config import settings
from app.db.base import Base, register_models
from app.db.session import get_db
from app.mesh import client as mesh_client
from app.retrieval import embeddings as embeddings_module
from app.retrieval import vector_store as vs_module

register_models()


@pytest.fixture(autouse=True)
def isolated_rate_limits():
    """
    Stage 16: `app/core/rate_limit.py` keeps its counters in a
    module-level dict — process-global, on purpose (see that module's
    docstring). Every test in this suite runs in the same process, so
    without resetting it, tests/test_rate_limit.py exhausting a
    "login"/"register" bucket would leak into whatever other test
    happens to run next and share that bucket+identifier (e.g. two
    tests both logging in from the TestClient's default "testclient"
    IP). Runs before AND after each test — before, so a prior test's
    leftover state never affects this one; after, for the same reason
    isolated_vector_store resets on both sides of the yield.
    """
    rate_limit.reset_all()
    yield
    rate_limit.reset_all()


@pytest.fixture(autouse=True)
def no_digest_scheduler(monkeypatch):
    """
    Stage 15: forces `settings.digest_enabled` off for the whole test
    suite. Empirically, `TestClient(app)` used without a `with` block
    (as the `client` fixture below does) never fires FastAPI's
    startup/shutdown events on this FastAPI/Starlette version — verified
    directly before writing this fixture — so app/main.py's
    `_start_digest_scheduler` wouldn't run during pytest even without
    this. This fixture exists anyway as an explicit, version-independent
    guarantee: if the test client, FastAPI, or this fixture file itself
    ever changes to use a context manager (matching real ASGI-server
    behavior more closely), a real APScheduler background thread
    scheduling real DB writes must still never start during a test run.
    """
    monkeypatch.setattr(settings, "digest_enabled", False)


@pytest.fixture(autouse=True)
def isolated_vector_store(tmp_path):
    """
    Every product create/update/archive/restore now triggers a vector
    write (Stage 4). Without this fixture, every test in the suite
    would share ONE on-disk Chroma collection while each test gets its
    own fresh, autoincrement-from-1 SQLite database — product #1 in one
    test would silently collide with product #1 in another. Autouse so
    no test has to remember to ask for isolation; it's not opt-in
    because forgetting it would produce flaky, order-dependent failures
    rather than a clean error.

    Stage 18 bugfix: this used to patch `settings.vector_db_path` via
    `monkeypatch.setattr`. That's tracked by the test's shared
    `monkeypatch` fixture instance, so tests/test_retrieval.py's
    failure-simulation tests calling `monkeypatch.undo()` mid-test (to
    end a simulated outage) were undoing THIS patch too — silently
    falling back to the real `settings.vector_db_path` (`./.chroma`) for
    the rest of the test. That's exactly the footgun
    `isolated_embedding_provider` below was already written to avoid
    (see its docstring) — this fixture just hadn't been given the same
    treatment yet. It stayed invisible as long as `./.chroma` happened
    to be a writable, uncorrupted directory; it surfaced as a hard
    `chromadb...InternalError: disk I/O error` once that on-disk store
    was in a bad state, which is a real bug regardless of what state
    `./.chroma` happens to be in — a test must never be able to touch
    it at all. Fixed the same way: plain assignment + manual restore,
    immune to any test's own `monkeypatch.undo()` call.
    """
    original = settings.vector_db_path
    settings.vector_db_path = str(tmp_path / "chroma")
    vs_module._client = None
    yield
    settings.vector_db_path = original
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


@pytest.fixture(autouse=True)
def no_real_mesh_chat_calls():
    """
    Stage 10 wires an LLM-generated narration into /dashboard
    (app/recommendations/narration.py), which calls mesh_client.chat().
    Without this fixture, every test that hits GET /dashboard — across
    tests/test_auth.py and tests/test_recommendations.py, none of which
    know or care about narration — would attempt a real Mesh chat call.

    Defaults `chat` to raising MeshAPIError, simulating "Mesh chat is
    unavailable." This isn't an arbitrary stub: it's the exact fallback
    path every other test implicitly relies on (no narration shown,
    dashboard renders normally), so this fixture is effectively also
    continuous regression coverage for that fallback.
    tests/test_narration.py overrides mesh_client.chat per-test to
    exercise the success/grounding paths deliberately.

    Same plain-assignment (not monkeypatch) pattern as
    isolated_embedding_provider, for the same monkeypatch.undo()-safety
    reason.
    """
    original = mesh_client.chat

    def _unavailable(*args, **kwargs):
        raise mesh_client.MeshAPIError("mesh chat not configured for this test")

    mesh_client.chat = _unavailable
    yield
    mesh_client.chat = original


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
