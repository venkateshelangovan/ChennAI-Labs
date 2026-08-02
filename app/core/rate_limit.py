"""
Stage 16: basic per-IP/per-user rate limiting.

Stage 0 Section 16's exact ask: "Auth endpoints and the manual
recommendation-refresh endpoint get basic per-IP/per-user rate
limiting to blunt brute-force and cost abuse." Through Stage 15,
`/login`, `/register`, `POST /dashboard/refresh`, and (added in Stage
15 itself) `POST /admin/recommendations/run-digest` had no generic
protection against a script hammering them — `/dashboard/refresh` had
Stage 12's cooldown, but that's a business rule keyed to a snapshot's
own `generated_at`, not a hard cap on requests, and login/register had
nothing at all.

--- Why in-memory, not Redis or a DB table ---

Same reasoning as Stage 0 Section 15's caching philosophy ("introduce
Redis only if a measured need arises," applied there to recommendation
caching): a plain in-process counter is the simplest thing that's
actually correct for how this app runs today — one `uvicorn` process,
no `--workers > 1`, no horizontal replicas (Stage 19's deployment
sketch). The one honest limitation, stated plainly rather than
glossed over: this state is per-process and resets on restart, and
would under-count across multiple worker processes or replicas, since
each process keeps its own dict. THAT is the specific, concrete,
measured need that would justify a shared store (Redis, or a DB table
with row-level counters) — not "might need it someday," which is
exactly the premature-infrastructure Section 15 warns against.

--- Why fixed-window, not sliding-window or token-bucket ---

Section 16 says "basic" and "blunt" — this doesn't need to be precise
down to the request, it needs to make a brute-force loop or an
AI-cost-abuse loop expensive enough to be pointless. A fixed window
(count resets to zero the instant `window_seconds` elapses since the
window started) is simpler to reason about and test than a sliding
window, at the cost of allowing a burst of up to 2x the limit right at
a window boundary — an acceptable trade for "blunt."
"""

import time

_windows: dict[str, tuple[int, float]] = {}  # "{bucket}:{identifier}" -> (count, window_started_at)


def allow(bucket: str, identifier: str, *, limit: int, window_seconds: int, now: float | None = None) -> bool:
    """
    Records a hit and returns True if `identifier` is still under
    `limit` within the current `window_seconds` window for this
    `bucket`. Returns False (without recording another hit) once the
    window's limit is exhausted — repeated calls while blocked don't
    extend the block, they just keep returning False until the window
    rolls over.
    """
    now = now if now is not None else time.monotonic()
    key = f"{bucket}:{identifier}"
    count, window_started_at = _windows.get(key, (0, now))

    if now - window_started_at >= window_seconds:
        count, window_started_at = 0, now  # window rolled over — start fresh

    if count >= limit:
        return False

    _windows[key] = (count + 1, window_started_at)
    return True


def reset_all() -> None:
    """
    Test-only. `_windows` is module-level, process-global state — every
    test in the suite runs in the same process, so without this,
    tests/test_rate_limit.py exhausting a bucket would leak into
    whatever test happens to run next and touch the same bucket/key.
    tests/conftest.py's autouse `isolated_rate_limits` fixture calls
    this before every test.
    """
    _windows.clear()
