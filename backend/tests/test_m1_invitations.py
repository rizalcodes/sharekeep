"""M1 invitation tests (Architecture.md §4.6, Schema.md §4).

The two rules under test that carry real weight:
  - `expires_at` is authoritative, not the `status` column
  - an invitation grants access to one email address, not to whoever holds the link
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.enums import InvitationStatus
from app.models.identity import WorkspaceInvitation
from tests.conftest import new_user, signup

WS = "/api/v1/workspaces"
INV = "/api/v1/invitations"


async def _workspace_with_owner(make_client, email: str, name: str = "Team"):
    owner, _ = await new_user(make_client, email)
    workspace_id = (await owner.post(WS, json={"name": name})).json()["id"]
    return owner, workspace_id


async def _invite(owner, workspace_id, email, role="editor"):
    response = await owner.post(
        f"{WS}/{workspace_id}/invitations", json={"email": email, "role": role}
    )
    assert response.status_code == 201, response.text
    return response.json()["token"], response.json()["invitation"]


# --- Creation --------------------------------------------------------------------

async def test_owner_can_invite_and_token_returned_once(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "inv-owner@example.com")
    token, invitation = await _invite(owner, workspace_id, "newbie@example.com")

    assert len(token) > 32
    assert invitation["status"] == "pending"
    assert invitation["email"] == "newbie@example.com"

    # Listing must never expose the token again.
    listed = (await owner.get(f"{WS}/{workspace_id}/invitations")).json()
    assert "token" not in listed[0]


async def test_raw_token_is_not_stored(make_client, db):
    """Only the hash is persisted — a database leak must not yield workspace access."""
    owner, workspace_id = await _workspace_with_owner(make_client, "inv-hash@example.com")
    token, _ = await _invite(owner, workspace_id, "hashed@example.com")

    result = await db.execute(select(WorkspaceInvitation))
    rows = result.scalars().all()
    assert rows
    for row in rows:
        assert row.token_hash != token
        assert len(row.token_hash) == 64


async def test_cannot_invite_an_existing_member(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "inv-dupe@example.com")
    invitee, _ = await new_user(make_client, "already@example.com")
    token, _ = await _invite(owner, workspace_id, "already@example.com")
    await invitee.post(f"{INV}/accept", json={"token": token})

    again = await owner.post(
        f"{WS}/{workspace_id}/invitations", json={"email": "already@example.com"}
    )
    assert again.status_code == 409


async def test_cannot_invite_directly_as_owner(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "inv-owner2@example.com")
    response = await owner.post(
        f"{WS}/{workspace_id}/invitations", json={"email": "x@example.com", "role": "owner"}
    )
    assert response.status_code == 422


async def test_reinviting_supersedes_the_previous_invitation(make_client, db):
    """The partial unique index allows only one live invitation per address."""
    owner, workspace_id = await _workspace_with_owner(make_client, "inv-again@example.com")
    first_token, _ = await _invite(owner, workspace_id, "twice@example.com")
    second_token, _ = await _invite(owner, workspace_id, "twice@example.com")

    assert first_token != second_token
    # The superseded token must no longer work.
    assert (await owner.post(f"{INV}/preview", json={"token": first_token})).status_code == 404
    assert (await owner.post(f"{INV}/preview", json={"token": second_token})).status_code == 200


# --- Acceptance ------------------------------------------------------------------

async def test_existing_user_accepts_and_becomes_a_member(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "acc-owner@example.com")
    invitee, _ = await new_user(make_client, "acc-invitee@example.com")
    token, _ = await _invite(owner, workspace_id, "acc-invitee@example.com", role="editor")

    response = await invitee.post(f"{INV}/accept", json={"token": token})
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"

    workspaces = (await invitee.get(WS)).json()
    joined = [w for w in workspaces if w["id"] == workspace_id]
    assert joined and joined[0]["role"] == "editor"


async def test_new_user_signs_up_through_the_invitation(make_client):
    """The accept-first flow: signup carries the token and joins in one step."""
    owner, workspace_id = await _workspace_with_owner(make_client, "flow-owner@example.com")
    token, _ = await _invite(owner, workspace_id, "brandnew@example.com", role="viewer")

    preview = await (await make_client()).post(f"{INV}/preview", json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["requires_signup"] is True
    assert preview.json()["email"] == "brandnew@example.com"

    invitee = await make_client()
    response = await signup(invitee, "brandnew@example.com", invitation_token=token)
    assert response.status_code == 201

    body = response.json()
    ids = {w["id"] for w in body["workspaces"]}
    assert workspace_id in ids, "signup with a token must join the workspace"
    # Personal workspace still created alongside.
    assert any(w["is_personal"] for w in body["workspaces"])
    # Holding the token proves control of the address.
    assert body["user"]["email_verified_at"] is not None


async def test_signup_token_overrides_a_mismatched_email(make_client):
    """Adversarial: a token holder must not be able to register a different address
    and still consume the invitation."""
    owner, workspace_id = await _workspace_with_owner(make_client, "override@example.com")
    token, _ = await _invite(owner, workspace_id, "intended@example.com")

    attacker = await make_client()
    response = await signup(attacker, "attacker@example.com", invitation_token=token)
    assert response.status_code == 201
    # The account is created against the INVITED address, not the one requested.
    assert response.json()["user"]["email"] == "intended@example.com"


async def test_accepting_with_a_different_account_is_refused(make_client):
    """Adversarial: forwarding the link must not grant access to someone else."""
    owner, workspace_id = await _workspace_with_owner(make_client, "mismatch-o@example.com")
    _, _ = await new_user(make_client, "intended-user@example.com")
    wrong_user, _ = await new_user(make_client, "wrong-user@example.com")
    token, _ = await _invite(owner, workspace_id, "intended-user@example.com")

    response = await wrong_user.post(f"{INV}/accept", json={"token": token})
    assert response.status_code == 403
    assert "different email" in response.json()["detail"].lower()

    # And they did not join.
    assert workspace_id not in {w["id"] for w in (await wrong_user.get(WS)).json()}


async def test_accept_requires_authentication(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "anon-acc@example.com")
    token, _ = await _invite(owner, workspace_id, "someone@example.com")

    anonymous = await make_client()
    assert (await anonymous.post(f"{INV}/accept", json={"token": token})).status_code == 401


async def test_garbage_token_is_refused(make_client):
    user, _ = await new_user(make_client, "garbage@example.com")
    assert (await user.post(f"{INV}/accept", json={"token": "not-a-real-token"})).status_code == 404
    preview = await user.post(f"{INV}/preview", json={"token": "not-a-real-token"})
    assert preview.status_code == 404


# --- Expiry and revocation (adversarial) -----------------------------------------

async def test_expired_invitation_is_refused(make_client, db):
    """`expires_at` is authoritative — status is still 'pending' here on purpose.

    This is the case that would pass if validation trusted the status column and the
    housekeeping sweep had not yet run.
    """
    owner, workspace_id = await _workspace_with_owner(make_client, "exp-owner@example.com")
    invitee, _ = await new_user(make_client, "expired-user@example.com")
    token, invitation = await _invite(owner, workspace_id, "expired-user@example.com")

    row = await db.get(WorkspaceInvitation, invitation["id"])
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.flush()
    assert row.status is InvitationStatus.PENDING, "status deliberately left pending"

    assert (await invitee.post(f"{INV}/accept", json={"token": token})).status_code == 403
    assert (await owner.post(f"{INV}/preview", json={"token": token})).status_code == 404
    assert workspace_id not in {w["id"] for w in (await invitee.get(WS)).json()}


async def test_expired_invitation_cannot_be_used_at_signup(make_client, db):
    owner, workspace_id = await _workspace_with_owner(make_client, "exp-signup@example.com")
    token, invitation = await _invite(owner, workspace_id, "late@example.com")

    row = await db.get(WorkspaceInvitation, invitation["id"])
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    latecomer = await make_client()
    response = await signup(latecomer, "late@example.com", invitation_token=token)
    assert response.status_code == 400


async def test_revoked_invitation_is_refused(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "rev-owner@example.com")
    invitee, _ = await new_user(make_client, "revoked-user@example.com")
    token, invitation = await _invite(owner, workspace_id, "revoked-user@example.com")

    assert (
        await owner.delete(f"{WS}/{workspace_id}/invitations/{invitation['id']}")
    ).status_code == 204

    assert (await invitee.post(f"{INV}/accept", json={"token": token})).status_code == 403
    assert workspace_id not in {w["id"] for w in (await invitee.get(WS)).json()}


async def test_invitation_default_ttl_is_seven_days(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "ttl@example.com")
    _, invitation = await _invite(owner, workspace_id, "ttl-user@example.com")

    expires = datetime.fromisoformat(invitation["expires_at"])
    delta = expires - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


async def test_owner_of_another_workspace_cannot_revoke_your_invitation(make_client):
    """Adversarial: invitation IDs must be scoped to their workspace."""
    alice, alice_ws = await _workspace_with_owner(make_client, "a-inv@example.com", "A")
    bob, bob_ws = await _workspace_with_owner(make_client, "b-inv@example.com", "B")
    _, invitation = await _invite(alice, alice_ws, "target@example.com")

    response = await bob.delete(f"{WS}/{bob_ws}/invitations/{invitation['id']}")
    assert response.status_code == 404

    # Alice's invitation is still usable.
    listed = (await alice.get(f"{WS}/{alice_ws}/invitations")).json()
    assert listed[0]["status"] == "pending"


async def test_preview_leaks_nothing_beyond_the_invitation(make_client):
    owner, workspace_id = await _workspace_with_owner(make_client, "leak@example.com", "Secret Co")
    token, _ = await _invite(owner, workspace_id, "guest@example.com")

    body = (await (await make_client()).post(f"{INV}/preview", json={"token": token})).json()
    assert set(body) == {"workspace_name", "email", "role", "expires_at", "requires_signup"}
    assert body["workspace_name"] == "Secret Co"
