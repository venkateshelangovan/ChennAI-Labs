"""
Stage 9's critical path for the Mesh client itself: request shape,
retry-on-transient-failure, no-retry-on-permanent-failure, and that a
missing API key fails fast without ever touching the network.

There is no real Mesh API to test against (api.meshapi.ai isn't a real,
reachable service for this project — see app/mesh/client.py's module
docstring), so every test here replaces `httpx` inside app.mesh.client
with a fake that returns scripted responses, and replaces `time.sleep`
with a no-op that just records how long it *would* have waited — these
tests need to run in milliseconds, not actually sit through exponential
backoff.
"""

import httpx as real_httpx
import pytest

from app.core.config import settings
from app.mesh import client as mesh_client


class FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        return self._json_body


class FakeHttpx:
    """Stands in for the `httpx` module inside app.mesh.client's namespace."""

    RequestError = real_httpx.RequestError  # the except clause needs this to still resolve

    def __init__(self, post_fn):
        self.calls = []  # list of (args, kwargs) tuples, one per call
        original = post_fn

        def recording_post(*args, **kwargs):
            self.calls.append((args, kwargs))
            return original(*args, **kwargs)

        self.post = recording_post


@pytest.fixture(autouse=True)
def mesh_configured(monkeypatch):
    monkeypatch.setattr(settings, "mesh_api_key", "test-key")
    monkeypatch.setattr(settings, "mesh_base_url", "https://mesh.test/v1")


@pytest.fixture()
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(mesh_client.time, "sleep", lambda seconds: sleeps.append(seconds))
    return sleeps


def _install(monkeypatch, post_fn):
    fake = FakeHttpx(post_fn)
    monkeypatch.setattr(mesh_client, "httpx", fake)
    return fake


def test_embed_succeeds_on_first_attempt(monkeypatch, no_sleep):
    fake = _install(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": [{"embedding": [0.1, 0.2, 0.3]}]}))

    result = mesh_client.embed("hello world", model="test-model")

    assert result == [0.1, 0.2, 0.3]
    assert len(fake.calls) == 1
    assert no_sleep == []


def test_embed_sends_the_documented_request_shape(monkeypatch, no_sleep):
    fake = _install(monkeypatch, lambda *a, **k: FakeResponse(200, {"data": [{"embedding": [1.0]}]}))

    mesh_client.embed("hello world", model="test-model")

    args, kwargs = fake.calls[0]
    assert args[0] == "https://mesh.test/v1/embeddings"  # url is passed positionally
    assert kwargs["json"] == {"model": "test-model", "input": "hello world"}
    assert kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_embed_retries_on_5xx_then_succeeds(monkeypatch, no_sleep):
    responses = [FakeResponse(500, text="server error"), FakeResponse(200, {"data": [{"embedding": [9.0]}]})]

    def post_fn(*a, **k):
        return responses.pop(0)

    fake = _install(monkeypatch, post_fn)

    result = mesh_client.embed("retry me", model="test-model")

    assert result == [9.0]
    assert len(fake.calls) == 2
    assert no_sleep == [mesh_client.BACKOFF_BASE_SECONDS]


def test_embed_raises_after_exhausting_retries_on_5xx(monkeypatch, no_sleep):
    fake = _install(monkeypatch, lambda *a, **k: FakeResponse(503, text="still down"))

    with pytest.raises(mesh_client.MeshAPIError):
        mesh_client.embed("never works", model="test-model")

    assert len(fake.calls) == mesh_client.MAX_ATTEMPTS
    assert len(no_sleep) == mesh_client.MAX_ATTEMPTS - 1


def test_embed_does_not_retry_on_non_retryable_status(monkeypatch, no_sleep):
    fake = _install(monkeypatch, lambda *a, **k: FakeResponse(401, text="invalid api key"))

    with pytest.raises(mesh_client.MeshAPIError):
        mesh_client.embed("bad auth", model="test-model")

    assert len(fake.calls) == 1  # no retry budget spent on a config problem
    assert no_sleep == []


def test_embed_retries_on_connection_error_then_succeeds(monkeypatch, no_sleep):
    attempts = {"count": 0}

    def post_fn(*a, **k):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise real_httpx.ConnectError("connection refused")
        return FakeResponse(200, {"data": [{"embedding": [3.0]}]})

    _install(monkeypatch, post_fn)

    result = mesh_client.embed("flaky network", model="test-model")

    assert result == [3.0]
    assert attempts["count"] == 2


def test_embed_raises_without_network_call_when_api_key_missing(monkeypatch, no_sleep):
    monkeypatch.setattr(settings, "mesh_api_key", "")

    def post_fn(*a, **k):
        raise AssertionError("should never attempt a network call with no API key configured")

    fake = _install(monkeypatch, post_fn)

    with pytest.raises(mesh_client.MeshAPIError, match="not configured"):
        mesh_client.embed("no key", model="test-model")

    assert fake.calls == []


def test_embed_raises_on_malformed_response_shape(monkeypatch, no_sleep):
    _install(monkeypatch, lambda *a, **k: FakeResponse(200, {"unexpected": "shape"}))

    with pytest.raises(mesh_client.MeshAPIError):
        mesh_client.embed("weird response", model="test-model")
