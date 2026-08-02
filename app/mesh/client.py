"""
Stage 9/10: the centralized Mesh API client.

Per Stage 0's constraint, this is the ONLY module in the codebase
allowed to make an outbound call to an AI provider, and Mesh is the
ONLY provider ever called — never OpenAI/Anthropic/etc. directly.
Every caller goes through one of the two public functions below —
`embed` (Stage 9, via app.retrieval.embeddings.MeshEmbeddingProvider)
or `chat` (Stage 10, via app.recommendations.narration) — never
imports `httpx` or builds a Mesh request itself.

--- The wire contract, and why it's an assumption ---

Mesh is this project's stand-in for "an internal/managed AI gateway" —
nothing in Stage 0 specifies its actual HTTP contract, and
`https://api.meshapi.ai` is not a real, reachable service. Rather than
block on a spec that doesn't exist, this client implements the
contract almost every embeddings/chat-API-compatible gateway uses
(OpenAI's, which Azure OpenAI and most proxies in front of either one
also mirror for drop-in compatibility) — the reasonable default to
assume for an unspecified internal gateway rather than inventing
something novel:

    POST {MESH_BASE_URL}/embeddings
    {"model": "...", "input": "text to embed"}
    -> 200 {"data": [{"embedding": [0.1, 0.2, ...]}]}

    POST {MESH_BASE_URL}/chat/completions
    {"model": "...", "messages": [{"role": "...", "content": "..."}], "temperature": 0.3}
    -> 200 {"choices": [{"message": {"content": "generated text"}}]}

Both share the same `Authorization: Bearer {MESH_API_KEY}` header. If
the real Mesh API (should one exist for grading/deployment) uses a
different shape, `embed`/`chat`'s request construction and response
parsing are the only places that need to change — the shared
retry/backoff/error-handling in `_post` is contract-agnostic and
shouldn't need to move.

--- Retry policy ---

Retries only what a retry can plausibly fix: connection errors,
timeouts, and 5xx responses (the server's problem, possibly transient).
A 4xx is never retried — an invalid API key or malformed request needs
a human or a config fix; retrying just delays a failure that's going to
happen anyway. Missing configuration (no API key at all) fails
immediately with no network attempt for the same reason — it's in the
same class of "not going to succeed on retry" as a 4xx, and skipping
the attempt entirely also means local development without real Mesh
credentials fails fast instead of waiting through a full retry/backoff
cycle for every call.

Backoff is exponential (0.5s, 1s, ...), not because a delay ever fixes
a genuinely broken service, but because a burst of retries hitting a
struggling one at full speed makes it worse, not better.
"""

import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger("chennai_labs.mesh")

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}


class MeshAPIError(Exception):
    """
    Raised for any Mesh API failure — missing config, a non-retryable
    status, or retry exhaustion. A single exception type (rather than
    one per failure mode) is deliberate: every caller in this codebase
    (app/retrieval/sync.py's sync_product for `embed`,
    app/recommendations/narration.py for `chat`) already handles "the
    call failed" uniformly — catch, log, fall back to the deterministic
    path — and doesn't need to distinguish *why* it failed to do the
    right thing.
    """


def _post(endpoint: str, payload: dict) -> dict:
    """
    Shared retry/backoff/error-handling for any Mesh JSON endpoint.
    `embed` and `chat` are just "build this payload, call this
    endpoint, parse the response this way" wrappers around this.
    """
    if not settings.mesh_api_key:
        raise MeshAPIError("MESH_API_KEY is not configured — see .env.example")

    url = f"{settings.mesh_base_url.rstrip('/')}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {settings.mesh_api_key}",
        "Content-Type": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "mesh_request_error",
                extra={"endpoint": endpoint, "attempt": attempt, "max_attempts": MAX_ATTEMPTS, "error": str(exc)},
            )
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            if response.status_code == 200:
                logger.info(
                    "mesh_request_succeeded", extra={"endpoint": endpoint, "attempt": attempt, "duration_ms": duration_ms}
                )
                return response.json()

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = MeshAPIError(f"Mesh API returned {response.status_code}: {response.text[:200]}")
                logger.warning(
                    "mesh_request_retryable_error",
                    extra={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
            else:
                # A 4xx (or any other non-5xx failure) is a config/request
                # problem, not a transient one — fail immediately rather
                # than burn the remaining retry budget on it.
                logger.error(
                    "mesh_request_non_retryable_error",
                    extra={
                        "endpoint": endpoint,
                        "attempt": attempt,
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )
                raise MeshAPIError(
                    f"Mesh API returned non-retryable status {response.status_code}: {response.text[:200]}"
                )

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise MeshAPIError(f"Mesh API request to {endpoint} failed after {MAX_ATTEMPTS} attempts") from last_error


def embed(text: str, *, model: str) -> list[float]:
    """Stage 9. The embeddings endpoint."""
    body = _post("embeddings", {"model": model, "input": text})
    try:
        return body["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MeshAPIError(f"Unexpected Mesh embeddings response shape: {body!r}") from exc


def chat(messages: list[dict], *, model: str, temperature: float = 0.3) -> str:
    """
    Stage 10. The chat/completion endpoint — used only for generating
    the recommendation narration (app/recommendations/narration.py).
    Low default temperature: this is explanatory copy grounded in real
    data, not creative writing — it should read the same way twice
    given the same recommendations, not vary for its own sake.
    """
    body = _post("chat/completions", {"model": model, "messages": messages, "temperature": temperature})
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MeshAPIError(f"Unexpected Mesh chat response shape: {body!r}") from exc
