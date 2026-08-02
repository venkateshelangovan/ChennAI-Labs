"""
Stage 17: app/core/scheduler.py had zero direct test coverage through
Stage 16 — its actual startup/shutdown was only ever exercised via live
verification (a real `uvicorn` process, since `TestClient` never fires
FastAPI lifespan events on this stack — see tests/conftest.py's
`no_digest_scheduler` docstring). That's the right way to prove the
real APScheduler thread behaves correctly, but it left the module's own
logic — job registration, start-is-idempotent, shutdown-clears-state,
shutdown-when-never-started-is-a-no-op — with no fast, deterministic
test at all.

Here, `BackgroundScheduler` itself is replaced with a lightweight fake
that records what was called rather than spinning up a real thread —
this file is testing OUR wiring logic (do we call add_job with the
right trigger config, do we start it, does calling start twice reuse
the same instance), not APScheduler's own scheduling correctness,
which is APScheduler's test suite's job, not ours.
"""

import pytest

from app.core import scheduler as scheduler_module


class _FakeScheduler:
    def __init__(self, *, timezone=None):
        self.timezone = timezone
        self.jobs = []
        self.started = False
        self.shutdown_called_with = None

    def add_job(self, func, *, trigger, hour, minute, id, replace_existing):
        self.jobs.append(
            {"func": func, "trigger": trigger, "hour": hour, "minute": minute, "id": id, "replace_existing": replace_existing}
        )

    def start(self):
        self.started = True

    def shutdown(self, wait):
        self.shutdown_called_with = wait


@pytest.fixture(autouse=True)
def reset_scheduler_state(monkeypatch):
    """`_scheduler` is module-level state, same category as
    rate_limit's `_windows` — must not leak between tests."""
    monkeypatch.setattr(scheduler_module, "_scheduler", None)
    yield
    monkeypatch.setattr(scheduler_module, "_scheduler", None)


def test_start_scheduler_registers_the_daily_cron_job(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", lambda **kwargs: fake)
    monkeypatch.setattr(scheduler_module.settings, "digest_hour_utc", 6)

    result = scheduler_module.start_scheduler()

    assert result is fake
    assert fake.started is True
    assert len(fake.jobs) == 1
    job = fake.jobs[0]
    assert job["func"] is scheduler_module._run_digest_job
    assert job["trigger"] == "cron"
    assert job["hour"] == 6
    assert job["minute"] == 0
    assert job["id"] == "daily_digest"
    assert job["replace_existing"] is True


def test_start_scheduler_is_idempotent(monkeypatch):
    created = []

    def _factory(**kwargs):
        fake = _FakeScheduler(**kwargs)
        created.append(fake)
        return fake

    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", _factory)

    first = scheduler_module.start_scheduler()
    second = scheduler_module.start_scheduler()

    assert first is second
    assert len(created) == 1  # the second call never constructed a new scheduler


def test_shutdown_scheduler_stops_and_clears_state(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(scheduler_module, "BackgroundScheduler", lambda **kwargs: fake)
    scheduler_module.start_scheduler()

    scheduler_module.shutdown_scheduler()

    assert fake.shutdown_called_with is False  # wait=False — never block app shutdown on it
    assert scheduler_module._scheduler is None


def test_shutdown_scheduler_when_never_started_is_a_noop():
    scheduler_module.shutdown_scheduler()  # must not raise
    assert scheduler_module._scheduler is None


def test_run_digest_job_opens_and_closes_its_own_session(monkeypatch):
    """`_run_digest_job` is what the cron trigger actually calls — it
    must open its own DB session (there's no request to inherit one
    from) and close it even if the digest itself raises."""
    calls = {"opened": 0, "closed": 0, "ran_with": None}

    class _FakeSession:
        def close(self):
            calls["closed"] += 1

    def fake_session_local():
        calls["opened"] += 1
        return _FakeSession()

    def fake_run_daily_digest(db):
        calls["ran_with"] = db

    monkeypatch.setattr(scheduler_module, "SessionLocal", fake_session_local)
    monkeypatch.setattr(scheduler_module, "run_daily_digest", fake_run_daily_digest)

    scheduler_module._run_digest_job()

    assert calls["opened"] == 1
    assert calls["closed"] == 1
    assert calls["ran_with"] is not None


def test_run_digest_job_closes_session_even_if_digest_raises(monkeypatch):
    calls = {"closed": 0}

    class _FakeSession:
        def close(self):
            calls["closed"] += 1

    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: _FakeSession())

    def _boom(db):
        raise RuntimeError("simulated digest crash")

    monkeypatch.setattr(scheduler_module, "run_daily_digest", _boom)

    with pytest.raises(RuntimeError):
        scheduler_module._run_digest_job()

    assert calls["closed"] == 1
