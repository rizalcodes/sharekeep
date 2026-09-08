"""M1 authentication tests.

Security-sensitive paths get an adversarial case, not just a happy path (Rules.md §6).
"""

from __future__ import annotations

import logging

import pytest

from app.core import security
from app.core.logging import redact
from tests.conftest import DEFAULT_PASSWORD, signup

# --- Signup ----------------------------------------------------------------------

async def test_signup_creates_personal_workspace(client):
    response = await signup(client, "alice@example.com", full_name="Alice")
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["user"]["email"] == "alice@example.com"
    # PRD §8: exactly one personal workspace, role owner, created automatically.
    assert len(body["workspaces"]) == 1
    workspace = body["workspaces"][0]
    assert workspace["is_personal"] is True
    assert workspace["role"] == "owner"


async def test_signup_sets_httponly_cookies_and_no_tokens_in_body(client):
    response = await signup(client, "cookie@example.com")
    body = response.json()

    # Architecture.md §9: tokens live in httpOnly cookies, never in a readable body.
    assert "access_token" not in body
    assert "refresh_token" not in body

    cookies = {c.name: c for c in client.cookies.jar}
    assert security.ACCESS_COOKIE_NAME in cookies
    assert security.REFRESH_COOKIE_NAME in cookies

    set_cookie_headers = response.headers.get_list("set-cookie")
    assert all("HttpOnly" in header for header in set_cookie_headers)
    # Lax, not Strict: Strict would withhold the cookie on the cross-site navigation
    # from an emailed invitation link (Architecture.md §4.0).
    assert all("SameSite=lax" in header.lower().replace("samesite=lax", "SameSite=lax")
               or "samesite=lax" in header.lower() for header in set_cookie_headers)


async def test_signup_rejects_duplicate_email(client, make_client):
    await signup(client, "dupe@example.com")
    other = await make_client()
    response = await signup(other, "dupe@example.com")
    assert response.status_code == 409


async def test_signup_rejects_weak_password(client):
    response = await signup(client, "weak@example.com", password="short")
    assert response.status_code == 422


async def test_signup_email_is_normalised(client):
    response = await signup(client, "MiXeD@Example.COM")
    assert response.json()["user"]["email"] == "mixed@example.com"


# --- Login -----------------------------------------------------------------------

async def test_login_succeeds_with_correct_password(client, make_client):
    await signup(client, "bob@example.com")
    fresh = await make_client()
    response = await fresh.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": DEFAULT_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "bob@example.com"


async def test_login_rejects_wrong_password(client, make_client):
    await signup(client, "carol@example.com")
    fresh = await make_client()
    response = await fresh.post(
        "/api/v1/auth/login",
        json={"email": "carol@example.com", "password": "wrong-password-entirely"},
    )
    assert response.status_code == 401


async def test_login_does_not_reveal_whether_an_account_exists(client, make_client):
    """Adversarial: a caller must not be able to enumerate registered addresses."""
    await signup(client, "known@example.com")

    a = await make_client()
    wrong_password = await a.post(
        "/api/v1/auth/login",
        json={"email": "known@example.com", "password": "definitely-not-the-one"},
    )
    b = await make_client()
    no_such_user = await b.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "definitely-not-the-one"},
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["detail"] == no_such_user.json()["detail"]


# --- Session lifecycle -----------------------------------------------------------

async def test_me_requires_authentication(make_client):
    anonymous = await make_client()
    assert (await anonymous.get("/api/v1/auth/me")).status_code == 401


async def test_refresh_rotates_the_token(client):
    await signup(client, "rotate@example.com")
    original = client.cookies.get(security.REFRESH_COOKIE_NAME)

    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 200

    rotated = client.cookies.get(security.REFRESH_COOKIE_NAME)
    assert rotated is not None and rotated != original


async def test_reusing_a_rotated_refresh_token_revokes_every_session(client, make_client):
    """Adversarial: a replayed refresh token means it was captured.

    Refusing just that token would leave the thief's freshly rotated token alive, so
    the whole chain is revoked.
    """
    await signup(client, "replay@example.com")
    stolen = client.cookies.get(security.REFRESH_COOKIE_NAME)

    assert (await client.post("/api/v1/auth/refresh")).status_code == 200
    victims_current = client.cookies.get(security.REFRESH_COOKIE_NAME)

    attacker = await make_client()
    attacker.cookies.set(security.REFRESH_COOKIE_NAME, stolen)
    replay = await attacker.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert "reuse" in replay.json()["detail"].lower()

    # The victim's live token must also now be dead.
    victim = await make_client()
    victim.cookies.set(security.REFRESH_COOKIE_NAME, victims_current)
    assert (await victim.post("/api/v1/auth/refresh")).status_code == 401


async def test_logout_actually_revokes_the_refresh_token(client, make_client):
    """A logout that only clears a cookie is not a logout."""
    await signup(client, "logout@example.com")
    token = client.cookies.get(security.REFRESH_COOKIE_NAME)

    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    # Someone who copied the cookie before logout must not be able to use it.
    replay = await make_client()
    replay.cookies.set(security.REFRESH_COOKIE_NAME, token)
    assert (await replay.post("/api/v1/auth/refresh")).status_code == 401


async def test_logout_all_revokes_other_sessions(client, make_client):
    await signup(client, "everywhere@example.com")

    second = await make_client()
    await second.post(
        "/api/v1/auth/login",
        json={"email": "everywhere@example.com", "password": DEFAULT_PASSWORD},
    )
    assert (await second.get("/api/v1/auth/me")).status_code == 200

    assert (await client.post("/api/v1/auth/logout-all")).status_code == 204
    # The other device's refresh token is dead, so it cannot renew.
    assert (await second.post("/api/v1/auth/refresh")).status_code == 401


async def test_refresh_token_jwt_cannot_be_used_as_an_access_token(client):
    """Adversarial: token type confusion must be refused."""
    await signup(client, "confusion@example.com")
    refresh_value = client.cookies.get(security.REFRESH_COOKIE_NAME)

    client.cookies.set(security.ACCESS_COOKIE_NAME, refresh_value)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_access_token_with_a_forged_signature_is_refused(client):
    await signup(client, "forged@example.com")
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "00000000-0000-0000-0000-000000000001", "type": "access", "exp": 9999999999},
        "not-the-real-secret",
        algorithm="HS256",
    )
    client.cookies.set(security.ACCESS_COOKIE_NAME, forged)
    assert (await client.get("/api/v1/auth/me")).status_code == 401


# --- Password handling -----------------------------------------------------------

def test_password_is_hashed_not_stored():
    hashed = security.hash_password(DEFAULT_PASSWORD)
    assert DEFAULT_PASSWORD not in hashed
    assert hashed.startswith("$argon2id$")
    assert security.verify_password(DEFAULT_PASSWORD, hashed)
    assert not security.verify_password("wrong", hashed)


def test_verify_password_against_missing_hash_is_false_not_an_error():
    """A login for an unknown email still runs a verification, so timing does not
    distinguish it — but it must never return True."""
    assert security.verify_password("anything", None) is False


@pytest.mark.parametrize(
    "message",
    [
        'password="correct-horse-battery-staple"',
        "login attempt with password: hunter2-hunter2",
        '{"email": "a@b.com", "password": "s3cret-passphrase"}',
    ],
)
def test_passwords_never_survive_log_redaction(message):
    """Rules.md §6 requires the redaction filter to cover password paths too."""
    cleaned = redact(message)
    for secret in ("correct-horse-battery-staple", "hunter2-hunter2", "s3cret-passphrase"):
        assert secret not in cleaned, f"password leaked into logs: {cleaned}"


async def test_login_does_not_log_the_password(client, caplog):
    """Adversarial: exercise the real endpoint and inspect everything it logged."""
    await signup(client, "logcheck@example.com")
    with caplog.at_level(logging.DEBUG):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "logcheck@example.com", "password": DEFAULT_PASSWORD},
        )
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert DEFAULT_PASSWORD not in combined


async def test_failed_login_audit_records_email_but_not_password(client, db):
    from sqlalchemy import select

    from app.models.enums import AuditAction
    from app.models.identity import AuditLog

    await client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@example.com", "password": "a-very-secret-value"},
    )
    result = await db.execute(
        select(AuditLog).where(AuditLog.action == AuditAction.LOGIN_FAILED)
    )
    entries = result.scalars().all()
    assert entries, "a failed login must be audited"
    serialised = str([entry.audit_metadata for entry in entries])
    assert "ghost@example.com" in serialised
    assert "a-very-secret-value" not in serialised
