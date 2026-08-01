"""
Creates an admin user directly against the database.

Deliberately NOT an HTTP endpoint: admin accounts must never be
self-service. If admin creation were a route, "create the first admin"
and "any authenticated attacker escalates their own role" become the
same code path unless guarded very carefully — running this out-of-band
as a script with direct database/filesystem access removes that whole
class of risk instead of relying on a guard to always be correct.

Usage:
    python -m scripts.create_admin admin@chennailabs.dev "Str0ng-Pass!" "Admin Name"
"""

import sys

from app.auth.service import EmailAlreadyRegistered, register_user
from app.db.session import SessionLocal


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python -m scripts.create_admin <email> <password> <display_name>")
        raise SystemExit(1)

    email, password, display_name = sys.argv[1:4]
    if len(password) < 8:
        print("Password must be at least 8 characters.")
        raise SystemExit(1)

    db = SessionLocal()
    try:
        user = register_user(db, email=email, password=password, display_name=display_name, role="admin")
        print(f"Created admin: {user.email} (id={user.id})")
    except EmailAlreadyRegistered:
        print(f"A user with email '{email}' already exists.")
        raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
