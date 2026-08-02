"""
ChennAI Labs — FastAPI application entrypoint.

Stage 4 adds: a vector-store reachability check on /health, alongside
the existing database check. Still no behavioral tracking, no AI calls
— those come later (Stage 5, Stage 9).
"""

import logging
import time

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin.events_routes import router as admin_events_router
from app.admin.recommendations_routes import router as admin_recommendations_router
from app.admin.routes import router as admin_router
from app.auth.dependencies import get_current_user
from app.auth.routes import router as auth_router
from app.core.config import settings
from app.core.exceptions import NotAuthenticated, NotAuthorized
from app.core.logging import configure_logging
from app.core.scheduler import shutdown_scheduler, start_scheduler
from app.db.session import SessionLocal, get_db
from app.events.routes import router as events_router
from app.products import service as product_service
from app.products.routes import router as products_router
from app.profile.routes import router as profile_router
from app.retrieval import vector_store
from app.templating import templates

configure_logging()
logger = logging.getLogger("chennai_labs")

app = FastAPI(title=settings.app_name)

# Stage 16 perf: gzip the responses actually worth compressing — HTML
# pages (this is a server-rendered app, so most bytes on the wire are
# Jinja2 output) and the static CSS/JS bundles. `minimum_size` avoids
# paying compression overhead on tiny responses (e.g. a 20-byte
# redirect) where gzip's own framing overhead can exceed the savings.
app.add_middleware(GZipMiddleware, minimum_size=500)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(products_router)
app.include_router(admin_router)
app.include_router(admin_events_router)
app.include_router(admin_recommendations_router)
app.include_router(events_router)
app.include_router(profile_router)


@app.on_event("startup")
def _validate_production_config() -> None:
    """
    Stage 16 security review: through Stage 15, a misconfigured
    production deployment would fail silently (or not fail at all) —
    e.g. `APP_ENV=production` with the default, publicly-visible-in-
    this-repo `SESSION_SECRET` still set. This doesn't reject the boot
    (a hard failure here could turn a config typo into a full outage,
    which is its own risk), it logs a loud WARNING for each finding so
    it shows up in whatever the deployment's log aggregator surfaces,
    and is cheap to grep for before going live. Runs unconditionally in
    dev/test too (nothing here is expensive), but `is_production` gates
    every actual finding, so local development never sees noise.
    """
    if not settings.is_production:
        return
    if settings.session_secret == "dev-only-insecure-secret-change-me":
        logger.warning(
            "production_config_warning",
            extra={"finding": "SESSION_SECRET is still the insecure default value"},
        )
    if not settings.mesh_api_key:
        logger.warning(
            "production_config_warning",
            extra={"finding": "MESH_API_KEY is unset — recommendations will run in popularity-fallback only"},
        )
    if settings.database_url.startswith("sqlite"):
        logger.warning(
            "production_config_warning",
            extra={"finding": "DATABASE_URL still points at SQLite in production — see README's deployment section"},
        )


@app.on_event("startup")
def _start_digest_scheduler() -> None:
    """
    Stage 15 (bonus): starts the APScheduler background thread that
    runs the proactive daily digest (app/recommendations/digest.py).
    Gated by `settings.digest_enabled` so it can be turned off entirely
    via env var (e.g. `DIGEST_ENABLED=false`) without touching code —
    and, deliberately, tests/conftest.py's autouse `no_digest_scheduler`
    fixture forces this setting off for the whole test suite, so pytest
    never spins up a real background thread scheduling real DB writes
    24 hours in the future.
    """
    if settings.digest_enabled:
        start_scheduler()


@app.on_event("shutdown")
def _stop_digest_scheduler() -> None:
    shutdown_scheduler()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Minimal request logging. This is the seed of the observability
    strategy from Stage 0 (Section 17) — every request gets a
    structured log line with method, path, status, and latency. Later
    stages add recommendation-specific fields on top of this same
    pattern rather than inventing a new logging approach.
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "request_handled",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """
    Stage 16: defense-in-depth response headers, applied uniformly
    rather than per-route so nobody has to remember to add them to a
    new route later.

    - `X-Content-Type-Options: nosniff` — stops a browser from
      MIME-sniffing a response into executing as something other than
      its declared Content-Type (relevant for anything user-influenced
      that ever gets served back, like an uploaded file would be).
    - `X-Frame-Options: DENY` — this app has no legitimate reason to be
      framed by another site; blocks clickjacking outright rather than
      relying on CSP alone.
    - `Referrer-Policy: strict-origin-when-cross-origin` — a sane
      modern default; full URLs (which could include a search query in
      the path) aren't leaked to third-party referrers.
    - `Strict-Transport-Security` — only set when `is_production`, and
      deliberately never in dev: HSTS on `http://127.0.0.1` would tell
      the browser to force HTTPS on localhost, breaking local dev with
      no easy undo (it's cached by the browser, not just per-response).
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    """A page that requires login, hit without a valid session: send to /login."""
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(NotAuthorized)
async def handle_not_authorized(request: Request, exc: NotAuthorized):
    """A valid session without the required role: 403, not a redirect."""
    return JSONResponse(status_code=403, content={"detail": "Forbidden"})


@app.get("/health")
async def health() -> JSONResponse:
    """
    Liveness/readiness check. Checks the database connection and the
    vector store (Stage 4) the same way — cheap, local, free to call on
    every hit. Mesh (Stage 9) is deliberately checked differently: it's
    an external, metered API, and a liveness probe that makes a real
    (possibly billed) call to a third-party service on every check is a
    known anti-pattern — a health endpoint hit every few seconds by an
    orchestrator would otherwise burn real API quota just existing.
    Instead this reports whether Mesh is *configured* (an API key is
    present), not whether it's currently reachable, and — unlike the
    database or vector store — a missing/unreachable Mesh never
    degrades the overall status to 503: Stage 4's dual-write design
    means the app still functions without it (products just don't sync
    to the vector store, and Stage 8's recommendations fall back to the
    popularity ranking), which is exactly the local-dev-without-
    credentials case this needs to not treat as "the app is down."
    """
    checks = {"database": "ok", "vector_store": "ok", "mesh": "configured" if settings.mesh_api_key else "not_configured"}
    status_code = 200
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any DB failure means "not ready"
        checks["database"] = f"error: {exc}"
        status_code = 503

    try:
        vector_store.health_check()
    except Exception as exc:  # noqa: BLE001
        checks["vector_store"] = f"error: {exc}"
        status_code = 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if status_code == 200 else "degraded",
            "app": settings.app_name,
            "env": settings.app_env,
            "checks": checks,
        },
    )


@app.get("/")
async def index(request: Request, db: Session = Depends(get_db)):
    featured = product_service.list_products(db)[:6]
    current_user = get_current_user(request, db)
    return templates.TemplateResponse(
        request, "index.html", {"featured_products": featured, "current_user": current_user}
    )
