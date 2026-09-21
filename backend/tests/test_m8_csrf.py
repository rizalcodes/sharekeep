"""CSRF double-submit token (Architecture §4.0, M8).

Adversarial by construction: every test here uses a bare client that does NOT attach the
token for it, and plays the attacker's part — a cross-site page that can make the browser
send our cookies but cannot read our responses.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from app.core import csrf
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models.enums import AuditAction
from app.models.identity import AuditLog
from tests.conftest import DEFAULT_PASSWORD, new_user, upload_file


@pytest.fixture
async def bare(db):
    """A browser with no script of ours running in it: cookies, but no header logic."""

    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as instance:
        yield instance
    app.dependency_overrides.clear()


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def _signed_in(bare: httpx.AsyncClient, email: str) -> str:
    token = await _token(bare)
    response = await bare.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": DEFAULT_PASSWORD, "full_name": "CSRF"},
        headers={csrf.HEADER_NAME: token},
    )
    assert response.status_code == 201, response.text
    return response.json()["csrf_token"]


async def test_an_unsafe_request_without_the_header_is_refused(bare):
    await _signed_in(bare, "csrf-no-header@example.com")
    # The attacker's forged form: cookies ride along, the header cannot.
    response = await bare.post("/api/v1/workspaces", json={"name": "Forged"})
    assert response.status_code == 403
    assert response.json()["code"] == csrf.FAILURE_CODE


async def test_a_header_that_does_not_match_the_cookie_is_refused(bare):
    await _signed_in(bare, "csrf-mismatch@example.com")
    response = await bare.post(
        "/api/v1/workspaces",
        json={"name": "Guessed"},
        headers={csrf.HEADER_NAME: csrf.new_token()},
    )
    assert response.status_code == 403


async def test_a_header_without_a_cookie_is_refused(bare):
    token = await _token(bare)
    bare.cookies.clear()
    response = await bare.post(
        "/api/v1/auth/login",
        json={"email": "x@example.com", "password": DEFAULT_PASSWORD},
        headers={csrf.HEADER_NAME: token},
    )
    assert response.status_code == 403


async def test_the_matching_pair_is_accepted(bare):
    token = await _signed_in(bare, "csrf-ok@example.com")
    response = await bare.post(
        "/api/v1/workspaces", json={"name": "Legitimate"}, headers={csrf.HEADER_NAME: token}
    )
    assert response.status_code == 201, response.text


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
async def test_every_unsafe_method_is_covered(bare, method):
    await _signed_in(bare, f"csrf-{method.lower()}@example.com")
    response = await bare.request(method, "/api/v1/workspaces/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 403


async def test_sign_in_itself_is_protected(bare):
    """Login CSRF: logging a victim into the attacker's account needs no victim session."""
    response = await bare.post(
        "/api/v1/auth/login", json={"email": "x@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 403


async def test_a_token_from_before_sign_in_is_worthless_after_it(bare):
    before = await _token(bare)
    after = await _signed_in(bare, "csrf-rotate@example.com")
    assert after != before
    response = await bare.post(
        "/api/v1/workspaces", json={"name": "Stale"}, headers={csrf.HEADER_NAME: before}
    )
    assert response.status_code == 403


async def test_sign_out_replaces_the_token(bare):
    token = await _signed_in(bare, "csrf-logout@example.com")
    response = await bare.post("/api/v1/auth/logout", headers={csrf.HEADER_NAME: token})
    assert response.status_code == 204
    assert bare.cookies.get(csrf.cookie_name()) != token


async def test_refresh_keeps_the_token_so_requests_in_flight_survive(bare):
    token = await _signed_in(bare, "csrf-refresh@example.com")
    response = await bare.post("/api/v1/auth/refresh", headers={csrf.HEADER_NAME: token})
    assert response.status_code == 200, response.text
    assert response.json()["csrf_token"] == token


async def test_safe_methods_need_no_token(bare):
    await _signed_in(bare, "csrf-get@example.com")
    assert (await bare.get("/api/v1/workspaces")).status_code == 200


async def test_the_refusal_carries_cors_headers_so_the_frontend_can_read_it(bare):
    """Outside CORS, a refusal reaches the browser as an opaque network failure, and the
    frontend could not tell a stale token from the server being down."""
    response = await bare.post(
        "/api/v1/auth/login",
        json={},
        headers={"Origin": settings.frontend_origin},
    )
    assert response.status_code == 403
    assert response.headers.get("access-control-allow-origin") == settings.frontend_origin


async def test_the_token_is_not_readable_by_script(bare):
    response = await bare.get("/api/v1/auth/csrf")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "domain=" not in cookie


def test_the_host_prefix_is_used_whenever_the_browser_would_honour_it(monkeypatch):
    monkeypatch.setattr(settings, "cookie_secure", True)
    monkeypatch.setattr(settings, "cookie_domain", None)
    assert csrf.cookie_name() == "__Host-fileshare_csrf"
    # A Domain attribute makes the browser reject a __Host- cookie outright, so the prefix
    # must not be claimed where it cannot be honoured.
    monkeypatch.setattr(settings, "cookie_domain", "sharekeep.xyz")
    assert csrf.cookie_name() == "fileshare_csrf"


def test_the_health_check_is_not_under_the_guard():
    assert not "/health/ready".startswith(settings.api_v1_prefix)


async def test_issuing_a_download_url_is_not_reachable_by_get(make_client, db):
    """A cross-site top-level GET still carries a Lax cookie. As a GET, any page could
    make a signed-in visitor's browser write a "downloaded" entry in their name."""
    owner, session = await new_user(make_client, "csrf-download@example.com")
    ws = session["workspaces"][0]["id"]
    file_id = (await upload_file(owner, ws, "report.txt")).json()["id"]

    async def downloads() -> int:
        return await db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == AuditAction.DOWNLOAD)
        )

    before = await downloads()
    forged = await owner.get(f"/api/v1/workspaces/{ws}/files/{file_id}/download")
    assert forged.status_code == 405
    assert await downloads() == before

    real = await owner.post(f"/api/v1/workspaces/{ws}/files/{file_id}/download")
    assert real.status_code == 200, real.text
    assert await downloads() == before + 1
