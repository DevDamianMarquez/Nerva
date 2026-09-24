"""Setting and clearing the browser session cookie.

`SameSite=Lax` is enough to stop cross-site forged requests (a genuine cross-site page
cannot make the browser attach the cookie to a state-changing request), without a
separate CSRF token, as long as the frontend and API are considered the same *site*.
In development that means opening the app at `http://127.0.0.1:3000` while the API runs
on `http://127.0.0.1:8000`: same host, different port, which browsers treat as
same-site. `Secure` is on in production and off in local development, since local
development has no TLS. See docs/ARCHITECTURE.md for the full reasoning, including what
changes if the frontend and API ever live on different top-level domains in production.
"""

from fastapi import Response

from nerva.core.config import Settings
from nerva.core.security import SESSION_COOKIE_NAME, SESSION_TTL


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
