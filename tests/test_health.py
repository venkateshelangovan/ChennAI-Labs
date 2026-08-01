"""
Stage 1's critical path is simply: the app boots and /health responds
correctly. Every later stage adds its own tests for its own critical
path (auth, catalog, dual-write, grounding, ...) rather than piling
onto this file.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "ChennAI Labs"


def test_index_renders_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ChennAI Labs" in response.text
