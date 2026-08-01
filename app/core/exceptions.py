"""
Auth-related exceptions raised by dependencies (app/auth/dependencies.py)
and translated into real HTTP responses by handlers registered in
app/main.py. Routes never catch these themselves — a route that depends
on `require_user` just gets a `User` back or the request never reaches
the route body at all.
"""


class NotAuthenticated(Exception):
    """No valid session. Handled by redirecting to /login."""


class NotAuthorized(Exception):
    """Valid session, wrong role. Handled by a 403."""
