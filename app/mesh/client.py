"""
Stage 9: the centralized Mesh API client.

Per Stage 0's constraint, this is the ONLY module in the codebase
allowed to make an outbound call to an AI provider, and Mesh is the
ONLY provider ever called — never OpenAI/Anthropic/etc. directly.
Every other module that needs an embedding goes through
app.retrieval.embeddings.get_embedding_provider(), never imports this
module directly; MeshEmbeddingProvider (in embeddings.py) is the one
caller.

--- The wire contract, and why it's an assumption ---

Mesh is this project's stand-in for "an internal/managed AI gateway" —
nothing in Stage 0 specifies its actual HTTP contract, and
`https://api.meshapi.ai` is not a real, reachable service. Rather than
block Stage 9 on a spec that doesn't exist, this client implements the
contract almost every embeddings-API-compatible gateway uses (OpenAI's,
which Azure OpenAI and most proxies in front of either one also mirror
for drop-in compatibility) — the reasonable default to assume for an
unspecified internal gateway rather than inventing something novel:

    POST {MESH_BASE_URL}/embeddings
    Authorization: Bearer {MESH_API_KEY}
    Content-Type: application/json
    {"model": "...", "input": "text to embed"}

    -> 200 {"data": [{"embedding": [0.1, 0.2, ...]}]}

If the real Mesh API (should one exist for grading/deployment) uses a
different shape, `_post_embeddings`'s request construction and
`embed`'s response parsing are the only two places that need to
change — the retry/backoff/error-handling logic below is contract-
agnostic and shouldn't need to move.

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
cycle for every single product sync.

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
    (app/retrieval/sync.py's sync_product) already handles "the
    embedding call failed" uniformly — catch, log, mark
    vector_sync_status='failed', don't roll back SQL — and doesn't need
    to distinguish *why* it failed to do the right thing.
    """


def _post_embeddings(text: str, *, model: str) -> dict:
    if not settings.mesh_api_key:
        raise MeshAPIError("MESH_API_KEY is not configured — see .env.example")

    url = f"{settings.mesh_base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {settings.mesh_api_key}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "input": text}

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        start = time.perf_counter()
        try:
            response = httpx.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS)
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning(
                "mesh_request_error",
                extra={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "error": str(exc)},
            )
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            if response.status_code == 200:
                logger.info("mesh_request_succeeded", extra={"attempt": attempt, "duration_ms": duration_ms})
                return response.json()

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = MeshAPIError(f"Mesh API returned {response.status_code}: {response.text[:200]}")
                logger.warning(
                    "mesh_request_retryable_error",
                    extra={"attempt": attempt, "status_code": response.status_code, "duration_ms": duration_ms},
                )
            else:
                # A 4xx (or any other non-5xx failure) is a config/request
                # problem, not a transient one — fail immediately rather
                # than burn the remaining retry budget on it.
                logger.error(
                    "mesh_request_non_retryable_error",
                    extra={"attempt": attempt, "status_code": response.status_code, "duration_ms": duration_ms},
                )
                raise MeshAPIError(
                    f"Mesh API returned non-retryable status {response.status_code}: {response.text[:200]}"
                )

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise MeshAPIError(f"Mesh API request failed after {MAX_ATTEMPTS} attempts") from last_error


def embed(text: str, *, model: str) -> list[float]:
    """The only public function — everything above is an implementation detail."""
    body = _post_embeddings(text, model=model)
    try:
        return body["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MeshAPIError(f"Unexpected Mesh API response shape: {body!r}") from exc
