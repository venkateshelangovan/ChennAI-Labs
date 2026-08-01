"""
ChennAI Labs — FastAPI application entrypoint.

Stage 3 adds: the products router (public catalog + admin CRUD) and a
homepage that features real catalog data instead of a placeholder.
Still no behavioral tracking, no vector store, no AI — those come later.
"""

import logging
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin.routes import router as admin_router
from app.auth.dependencies import get_current_user
from app.auth.routes import router as auth_router
from app.core.config import settings
from app.core.exceptions import NotAuthenticated, NotAuthorized
from app.core.logging import configure_logging
from app.db.session import SessionLocal, get_db
from app.products import service as product_service
from app.products.routes import router as products_router
from app.templating import templates

configure_logging()
logger = logging.getLogger("chennai_labs")

app = FastAPI(title=settings.app_name)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(products_router)
app.include_router(admin_router)


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
    Liveness/readiness check. Now checks the database connection —
    the first real dependency the app has. Stage 4 adds a vector-store
    check the same way; Stage 9 adds Mesh reachability. Each dependency
    failing shows up here individually rather than as an opaque 500
    somewhere else in the app.
    """
    checks = {"database": "ok"}
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
