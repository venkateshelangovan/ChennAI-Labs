"""
HTTP layer for auth: render forms, parse submissions, delegate every
real decision to app/auth/service.py, and translate the result into a
response (re-rendered form with errors, or a redirect + cookie).

Every state-changing POST here is protected by the double-submit CSRF
check (app/core/csrf.py) — GET requests that render a form mint/reuse a
token and stamp it into both the cookie and the hidden field; the POST
handler rejects the submission if they don't match.
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import service
from app.auth.dependencies import SESSION_COOKIE_NAME, get_current_user, require_user
from app.core.config import settings
from app.core.csrf import issue_csrf_token, set_csrf_cookie, verify_csrf
from app.db.models.user import User
from app.db.session import get_db
from app.events.schemas import TRACKING_SESSION_COOKIE
from app.events.service import reconcile_session
from app.recommendations.cache import get_dashboard_recommendations
from app.templating import templates

router = APIRouter()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@router.get("/register")
async def register_form(request: Request, db: Session = Depends(get_db)):
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "auth/register.html",
        {"errors": [], "form": {}, "csrf_token": token, "current_user": get_current_user(request, db)},
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    display_name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    errors: list[str] = []

    if not verify_csrf(request, csrf_token):
        errors.append("Your form session expired. Please try again.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != confirm_password:
        errors.append("Passwords do not match.")
    if not display_name.strip():
        errors.append("Display name is required.")

    user = None
    if not errors:
        try:
            user = service.register_user(
                db, email=email, password=password, display_name=display_name
            )
        except service.EmailAlreadyRegistered:
            errors.append("An account with that email already exists. Try logging in instead.")

    if errors or user is None:
        token = issue_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "auth/register.html",
            {
                "errors": errors,
                "form": {"email": email, "display_name": display_name},
                "csrf_token": token,
                "current_user": None,
            },
            status_code=422,
        )
        set_csrf_cookie(response, token)
        return response

    session = service.create_session(db, user)
    # Attach any behavior this browser generated before registering
    # (Stage 0, Journey 1) — reads the tracker's cookie, not a form
    # field, since it needs to work even if JS never touched this form.
    reconcile_session(db, request.cookies.get(TRACKING_SESSION_COOKIE), user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_days * 86400,
    )
    return response


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.get("/login")
async def login_form(request: Request, db: Session = Depends(get_db)):
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "auth/login.html",
        {"errors": [], "form": {}, "csrf_token": token, "current_user": get_current_user(request, db)},
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    errors: list[str] = []
    if not verify_csrf(request, csrf_token):
        errors.append("Your form session expired. Please try again.")

    user = None
    if not errors:
        try:
            user = service.authenticate_user(db, email=email, password=password)
        except service.InvalidCredentials:
            errors.append("Incorrect email or password.")

    if errors or user is None:
        token = issue_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "auth/login.html",
            {"errors": errors, "form": {"email": email}, "csrf_token": token, "current_user": None},
            status_code=422,
        )
        set_csrf_cookie(response, token)
        return response

    session = service.create_session(db, user)
    reconcile_session(db, request.cookies.get(TRACKING_SESSION_COOKIE), user.id)
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session.token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_ttl_days * 86400,
    )
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

@router.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    service.delete_session(db, token)
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# The logged-in landing page. Originally a placeholder that existed only
# to prove `require_user` gates access (Stage 2) — Stage 8 replaced that
# placeholder with the real output of the recommendation pipeline, and
# Stage 10 adds an LLM-generated narration ABOVE it. The recommendation
# list itself is untouched by the narration step — generate_narration
# only ever describes an already-final list, and returns text=None on
# any failure (Mesh unreachable, or a grounding check failure), which
# the template treats as "don't show a narration," never as an error.
#
# Stage 12: neither of those pipelines runs on every view anymore.
# get_dashboard_recommendations (app/recommendations/cache.py) decides,
# via app/recommendations/trigger.py's deterministic rules, whether the
# persisted RecommendationSnapshot is still good enough to serve as-is
# (zero Mesh calls) or needs regenerating. The template only cares about
# `.result` / `.narration`, identical in shape to what this route used
# to compute directly — a cache hit is invisible to the page itself.
# ---------------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    dashboard_recs = get_dashboard_recommendations(db, user.id)
    token = issue_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "current_user": user,
            "recommendations": dashboard_recs.result,
            "narration": dashboard_recs.narration,
            "cache_hit": dashboard_recs.cache_hit,
            "csrf_token": token,
        },
    )
    set_csrf_cookie(response, token)
    return response


@router.post("/dashboard/refresh")
async def dashboard_refresh(
    request: Request,
    csrf_token: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """
    Manual trigger condition (c) from Stage 0 Section 14 — same
    CSRF-protected POST-then-redirect pattern as /admin/products/sync.
    Rate-limited via trigger.py's cooldown rather than real per-IP/user
    rate limiting (Stage 16, not built yet) — see that module's
    docstring for why reusing the snapshot's own timestamp is enough
    for now.
    """
    if verify_csrf(request, csrf_token):
        get_dashboard_recommendations(db, user.id, manual_refresh=True)
    return RedirectResponse(url="/dashboard", status_code=303)
