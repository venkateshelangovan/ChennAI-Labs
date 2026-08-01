"""
Password hashing and session-token generation.

Why bcrypt directly rather than passlib: passlib's bcrypt backend has a
long-running compatibility issue with recent bcrypt releases (it probes
`bcrypt.__about__.__version__`, which newer bcrypt no longer ships,
producing a confusing warning/crash depending on version combo). Calling
`bcrypt` directly is one dependency instead of two, has no such issue,
and the API we actually need — hash, verify — is two functions.

Why bcrypt and not something faster (e.g. plain SHA-256): bcrypt is
deliberately slow and salts automatically per-hash, which is exactly the
property you want for password storage (resist brute-force/rainbow
tables) and exactly the property you don't want for anything else, which
is why it's used here and nowhere else in the codebase.
"""

import secrets

import bcrypt


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the DB — treat as "doesn't match" rather than 500.
        return False


def generate_session_token() -> str:
    """
    A high-entropy, unguessable opaque token — not a JWT, not derived
    from user data. Its only job is to be looked up against the
    `sessions` table; knowing it should be as good as knowing nothing
    without database access.
    """
    return secrets.token_urlsafe(32)
