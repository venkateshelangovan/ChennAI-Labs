"""
Stage 1's critical path is simply: the app boots and /health responds
correctly. Every later stage adds its own tests for its own critical
path (auth, catalog, dual-write, grounding, ...) rather than piling
onto this file.

Stage 17 fix: this file used to build its own module-level
`TestClient(app)` instead of using the shared `client` fixture
(tests/conftest.py) every other test file uses — which meant these two
tests were the only ones in the whole suite hitting the REAL
`settings.database_url` file (`chennai_labs.db`) rather than an
isolated in-memory database. That worked by accident as long as
someone had already run `alembic upgrade head` against that file
before `pytest`, and broke outright under parallel execution
(`pytest -n`) once multiple workers touched the same on-disk SQLite
file concurrently. Switched to the `client` fixture for the same
self-contained isolation every other test gets.
"""


def test_health_returns_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "ChennAI Labs"


def test_index_renders_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ChennAI Labs" in response.text
