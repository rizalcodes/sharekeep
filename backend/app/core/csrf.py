"""CSRF double-submit token (Architecture.md §4.0, specified at M8).

`SameSite=Lax` stops a cross-site POST from carrying our cookies, but not a request from
another host on the same registrable domain, and not a top-level GET. Every unsafe method
on the API therefore needs `X-CSRF-Token` equal to the value of an httpOnly cookie.

The token reaches the frontend in a response BODY (sign-in, sign-up, `GET /auth/csrf`),
never through a readable cookie, so no JavaScript on any subdomain can read it and the
cookie can stay host-only on the API host.

Why a matching pair is enough: an attacker's page can make the victim's browser send the
cookie, but cannot read our responses (CORS admits only the frontend origin) and so cannot
learn the value to put in the header. The one way around that is *planting* a cookie of
their own choosing from a sibling subdomain. In production the cookie is therefore named
with the `__Host-` prefix, which a browser refuses to accept with a `Domain` attribute —
so nothing but the API host itself can set it. The token is also replaced at every sign-in
and sign-out, so one obtained before signing in is worthless afterwards (login CSRF).
"""

from __future__ import annotations

import secrets

from fastapi import Response
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import settings

HEADER_NAME = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FAILURE_CODE = "csrf_failed"


def cookie_name() -> str:
    """`__Host-` requires Secure, Path=/ and no Domain; without all three the browser
    drops the cookie, so the prefix is used only where it can actually be honoured."""
    if settings.cookie_secure and not settings.cookie_domain:
        return "__Host-fileshare_csrf"
    return "fileshare_csrf"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=cookie_name(),
        value=token,
        # As long as a session can last: the token must not expire under a user who is
        # still signed in.
        max_age=settings.refresh_token_ttl_seconds,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def rotate(response: Response) -> str:
    """Issue a fresh token. Called at every change of who is signed in."""
    token = new_token()
    set_cookie(response, token)
    return token


def current_or_new(request: Request, response: Response) -> str:
    """The token this browser already holds, or a new one if it holds none."""
    token = request.cookies.get(cookie_name()) or new_token()
    set_cookie(response, token)
    return token


class CSRFMiddleware:
    """Refuse an unsafe API request whose header does not match its cookie.

    Pure ASGI rather than `BaseHTTPMiddleware`, so the request body is never read here
    and streaming is untouched. It must sit INSIDE the CORS middleware: a refusal without
    CORS headers reaches the browser as an unreadable network error, and the frontend
    could not tell a stale token from the server being down.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in UNSAFE_METHODS
            or not scope["path"].startswith(settings.api_v1_prefix)
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        cookie = request.cookies.get(cookie_name())
        header = request.headers.get(HEADER_NAME)
        if not cookie or not header or not secrets.compare_digest(cookie, header):
            reason = (
                "This request is missing its security token. Reload the page and try again."
                if not cookie or not header
                else "This request's security token is out of date. Reload the page and try again."
            )
            response = JSONResponse(
                {"detail": reason, "code": FAILURE_CODE}, status_code=403
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
