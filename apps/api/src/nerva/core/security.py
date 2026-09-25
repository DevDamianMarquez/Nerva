"""Password hashing and session tokens.

Sessions are opaque random tokens, not JWTs: the raw token lives only in the browser's
cookie, and the database stores its SHA-256 hash (the pattern GitHub uses for personal
access tokens), so a leaked database backup cannot be used to log in as anyone. This also
makes logout a real, immediate revocation, which a self-verifying JWT cannot offer without
a denylist of its own. See docs/ARCHITECTURE.md for the full rationale.
"""

import hashlib
import secrets
from datetime import timedelta

import bcrypt

SESSION_COOKIE_NAME = "nerva_session"
SESSION_TTL = timedelta(days=30)
# How long an OAuth `state` value is valid for; see nerva.services.integrations.
OAUTH_STATE_TTL = timedelta(minutes=10)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
