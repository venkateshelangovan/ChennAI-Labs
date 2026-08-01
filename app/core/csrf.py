"""
CSRF protection for server-rendered form posts, via the double-submit
cookie pattern.

How it works: when we render a page with a form, we set (or reuse) a
random token in an httponly cookie AND embed the same value as a hidden
form field. On submit, we compare the two. A cross-site request can
trick a browser into sending the cookie automatically, but it cannot
read the cookie's value to also put it in the hidden field (same-origin
policy) — so a forged submission has no way to make the two match.

This lives in `core/` rather than `auth/` because every future
state-changing form (product create/edit/delete in Stage 3+) needs the
same protection, not just login/register.

What this does NOT protect against: XSS. If an attacker can run
JavaScript on our own pages, they can read the DOM's hidden field
directly and the double-submit check becomes moot — that's a separate
concern, handled by Jinja2's autoescaping (Stage 0, Section 16) and by
never rendering unescaped user input. A stronger CSRF design (server-side
synchronizer token bound to the session, not just the cookie) is a
reasonable Stage 16 hardening upgrade if this ever needs to resist a
more sophisticated threat model.
"""

import secrets

from fastapi import Request
from starlette.responses import Response

CSRF_COOKIE_NAME = "csrf_token"


def issue_csrf_token(request: Request) -> str:
    """Reuse the token already on this browser if present, otherwise mint one."""
    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=False,  # flip to True once served over HTTPS in production
    )


def verify_csrf(request: Request, submitted_token: str) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not submitted_token:
        return False
    return secrets.compare_digest(cookie_token, submitted_token)
