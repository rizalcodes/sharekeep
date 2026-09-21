"""Invite-only sign-up in production (Architecture §4.0, PRD §10g decision 5)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.identity import User
from tests.conftest import new_user, signup

WS = "/api/v1/workspaces"


@pytest.fixture
def production(monkeypatch):
    def _set(allowlist: str = "") -> None:
        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "signup_allowlist", allowlist)

    return _set


async def _users(db) -> int:
    return await db.scalar(select(func.count()).select_from(User))


async def test_a_stranger_cannot_sign_up_in_production(client, db, production):
    production("owner@sharekeep.xyz")
    before = await _users(db)
    response = await signup(client, "stranger@example.com")
    assert response.status_code == 403
    assert "invitation" in response.json()["detail"].lower()
    assert await _users(db) == before


async def test_an_allowlisted_address_can_sign_up(client, production):
    production(" Owner@Sharekeep.xyz , second@example.com ")
    assert (await signup(client, "owner@sharekeep.xyz")).status_code == 201


async def test_an_empty_allowlist_means_invitation_only(client, production):
    production("")
    assert (await signup(client, "anyone@example.com")).status_code == 403


async def test_an_invitation_opens_sign_up_for_its_own_address(make_client, production):
    production("owner-gate@sharekeep.xyz")
    owner, _ = await new_user(make_client, "owner-gate@sharekeep.xyz")
    ws = (await owner.post(WS, json={"name": "Team"})).json()["id"]
    invited = await owner.post(
        f"{WS}/{ws}/invitations", json={"email": "guest@example.com", "role": "viewer"}
    )
    assert invited.status_code == 201, invited.text

    response = await signup(
        await make_client(), "guest@example.com", invitation_token=invited.json()["token"]
    )
    assert response.status_code == 201, response.text
    assert ws in {w["id"] for w in response.json()["workspaces"]}


async def test_the_gate_does_not_reveal_that_an_account_exists(make_client, production):
    """Checked before the duplicate check: a registered address outside the allowlist gets
    the same refusal as an unregistered one, not a 409 that confirms the account."""
    await new_user(make_client, "already-here@example.com")  # registered before the gate
    production("")

    registered = await signup(await make_client(), "already-here@example.com")
    unregistered = await signup(await make_client(), "never-here@example.com")
    assert registered.status_code == unregistered.status_code == 403
    assert registered.json() == unregistered.json()


async def test_sign_up_stays_open_outside_production(client):
    assert settings.environment != "production"
    assert (await signup(client, "local-dev@example.com")).status_code == 201
