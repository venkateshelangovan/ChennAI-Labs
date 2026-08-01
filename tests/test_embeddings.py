"""
Stage 9: get_embedding_provider() now defaults to Mesh, and
compute_content_hash is versioned so a future provider swap can't
silently leave stale vectors marked 'synced'. Neither test here makes
a real network call — constructing/selecting MeshEmbeddingProvider
doesn't call Mesh, only `.embed()` on it would (see test_mesh_client.py
for that), and this file never calls `.embed()` on the real provider.
"""

from app.core.config import settings
from app.mesh import client as mesh_client
from app.retrieval import embeddings as embeddings_module


def test_get_embedding_provider_defaults_to_mesh(monkeypatch):
    # tests/conftest.py's autouse isolated_embedding_provider fixture
    # forces the local placeholder for every other test in the suite —
    # this test undoes that just for itself, to prove what the factory
    # actually defaults to in real (non-test) use.
    monkeypatch.setattr(embeddings_module, "_provider", None)

    provider = embeddings_module.get_embedding_provider()

    assert isinstance(provider, embeddings_module.MeshEmbeddingProvider)


def test_get_embedding_provider_is_a_singleton(monkeypatch):
    monkeypatch.setattr(embeddings_module, "_provider", None)

    first = embeddings_module.get_embedding_provider()
    second = embeddings_module.get_embedding_provider()

    assert first is second


def test_mesh_embedding_provider_delegates_to_mesh_client(monkeypatch):
    captured = {}

    def fake_embed(text, *, model):
        captured["text"] = text
        captured["model"] = model
        return [1.0, 2.0]

    monkeypatch.setattr(mesh_client, "embed", fake_embed)
    monkeypatch.setattr(settings, "mesh_embedding_model", "mesh-embed-v1")

    result = embeddings_module.MeshEmbeddingProvider().embed("some course text")

    assert result == [1.0, 2.0]
    assert captured == {"text": "some course text", "model": "mesh-embed-v1"}


def test_content_hash_changes_when_embedding_schema_version_changes(monkeypatch):
    text = "Title: Some Course"
    original = embeddings_module.compute_content_hash(text)

    monkeypatch.setattr(embeddings_module, "EMBEDDING_SCHEMA_VERSION", "some-other-version")

    assert embeddings_module.compute_content_hash(text) != original


def test_content_hash_still_deterministic_for_same_version_and_text():
    text = "Title: Some Course"
    assert embeddings_module.compute_content_hash(text) == embeddings_module.compute_content_hash(text)
