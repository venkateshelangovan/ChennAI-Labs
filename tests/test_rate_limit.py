"""
Stage 16: app/core/rate_limit.py's fixed-window counter, tested in
isolation from any route. The route-level behavior (login/register
lockout, dashboard/refresh, admin run-digest) is covered separately in
tests/test_hardening.py — this file is only about the counting logic
itself: does it allow exactly `limit` hits, block the next one, and
recover once the window rolls over.
"""

from app.core import rate_limit


def test_allows_up_to_the_limit_within_the_window():
    for _ in range(3):
        assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0) is True


def test_blocks_the_hit_after_the_limit_is_reached():
    for _ in range(3):
        rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0)

    assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0) is False


def test_blocked_calls_do_not_extend_the_window_or_get_recorded():
    for _ in range(3):
        rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0)

    # Several more calls while blocked — none of these should be
    # recorded as hits, and none should change when the window resets.
    for _ in range(5):
        assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1010.0) is False

    # The window started at t=1000; it should roll over at t=1060, not
    # be pushed later by the blocked attempts at t=1010.
    assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1059.0) is False
    assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1060.0) is True


def test_window_resets_after_window_seconds_elapse():
    for _ in range(3):
        rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0)
    assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1000.0) is False

    # 60s later, a fresh window — allowed again, and a fresh budget of 3.
    for _ in range(3):
        assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1061.0) is True
    assert rate_limit.allow("bucket", "key", limit=3, window_seconds=60, now=1061.0) is False


def test_buckets_are_independent():
    for _ in range(3):
        rate_limit.allow("login", "1.2.3.4", limit=3, window_seconds=60, now=1000.0)
    assert rate_limit.allow("login", "1.2.3.4", limit=3, window_seconds=60, now=1000.0) is False

    # A different bucket for the same identifier has its own budget.
    assert rate_limit.allow("register", "1.2.3.4", limit=3, window_seconds=60, now=1000.0) is True


def test_identifiers_are_independent():
    for _ in range(3):
        rate_limit.allow("login", "1.2.3.4", limit=3, window_seconds=60, now=1000.0)
    assert rate_limit.allow("login", "1.2.3.4", limit=3, window_seconds=60, now=1000.0) is False

    # A different identifier (different client) in the same bucket has its own budget.
    assert rate_limit.allow("login", "5.6.7.8", limit=3, window_seconds=60, now=1000.0) is True


def test_reset_all_clears_every_bucket():
    rate_limit.allow("login", "1.2.3.4", limit=1, window_seconds=60, now=1000.0)
    assert rate_limit.allow("login", "1.2.3.4", limit=1, window_seconds=60, now=1000.0) is False

    rate_limit.reset_all()

    assert rate_limit.allow("login", "1.2.3.4", limit=1, window_seconds=60, now=1000.0) is True
