"""M1 workspace and RBAC tests (Architecture.md §4.6).

The isolation tests here are the ones that matter: this is the milestone where "User A
cannot touch User B's workspace" either becomes true or silently does not.
"""

from __future__ import annotations

import uuid

from tests.conftest import new_user

WS = "/api/v1/workspaces"


async def _invite_and_accept(owner, invitee_client, workspace_id, email, role="editor"):
    """Put a second user into a workspace, through the real endpoints."""
    created = await owner.post(
        f"{WS}/{workspace_id}/invitations", json={"email": email, "role": role}
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]
    accepted = await invitee_client.post("/api/v1/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    return token


# --- Creation and listing --------------------------------------------------------

async def test_create_additional_workspace(client):
    await new_user_inline(client, "creator@example.com")
    response = await client.post(WS, json={"name": "Team Space"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Team Space"
    assert body["is_personal"] is False
    assert body["role"] == "owner"


async def new_user_inline(client, email):
    from tests.conftest import signup

    response = await signup(client, email)
    assert response.status_code == 201, response.text
    return response.json()


async def test_list_returns_only_your_own_workspaces(make_client):
    alice, alice_session = await new_user(make_client, "a-list@example.com")
    bob, _ = await new_user(make_client, "b-list@example.com")

    await alice.post(WS, json={"name": "Alice Team"})

    bob_workspaces = (await bob.get(WS)).json()
    names = {w["name"] for w in bob_workspaces}
    assert "Alice Team" not in names
    # Bob sees only his own personal workspace.
    assert len(bob_workspaces) == 1
    assert bob_workspaces[0]["is_personal"] is True


# --- Cross-user isolation (adversarial) ------------------------------------------

async def test_user_cannot_read_another_users_workspace(make_client):
    alice, alice_session = await new_user(make_client, "a-read@example.com")
    bob, _ = await new_user(make_client, "b-read@example.com")
    alice_ws = alice_session["workspaces"][0]["id"]

    response = await bob.get(f"{WS}/{alice_ws}")
    # 404, not 403: a non-member must not learn that the workspace ID is real.
    assert response.status_code == 404


async def test_unknown_and_forbidden_workspaces_are_indistinguishable(make_client):
    alice, alice_session = await new_user(make_client, "a-probe@example.com")
    bob, _ = await new_user(make_client, "b-probe@example.com")
    alice_ws = alice_session["workspaces"][0]["id"]
    nonexistent = uuid.uuid4()

    forbidden = await bob.get(f"{WS}/{alice_ws}")
    missing = await bob.get(f"{WS}/{nonexistent}")
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()


async def test_user_cannot_modify_another_users_workspace(make_client):
    alice, alice_session = await new_user(make_client, "a-mod@example.com")
    bob, _ = await new_user(make_client, "b-mod@example.com")
    alice_ws = alice_session["workspaces"][0]["id"]

    assert (await bob.patch(f"{WS}/{alice_ws}", json={"name": "Pwned"})).status_code == 404
    assert (await bob.delete(f"{WS}/{alice_ws}")).status_code == 404
    assert (await bob.get(f"{WS}/{alice_ws}/members")).status_code == 404
    assert (
        await bob.post(f"{WS}/{alice_ws}/invitations", json={"email": "x@example.com"})
    ).status_code == 404

    # And the workspace is genuinely untouched.
    assert (await alice.get(f"{WS}/{alice_ws}")).json()["name"] != "Pwned"


async def test_unauthenticated_requests_are_rejected(make_client, client):
    _, session = await new_user(make_client, "auth-required@example.com")
    workspace_id = session["workspaces"][0]["id"]

    anonymous = await make_client()
    assert (await anonymous.get(WS)).status_code == 401
    assert (await anonymous.get(f"{WS}/{workspace_id}")).status_code == 401
    assert (await anonymous.post(WS, json={"name": "x"})).status_code == 401


# --- Role enforcement ------------------------------------------------------------

async def test_editor_cannot_perform_owner_actions(make_client):
    """Adversarial: a legitimate member must not exceed their role."""
    owner, owner_session = await new_user(make_client, "owner-role@example.com")
    editor, _ = await new_user(make_client, "editor-role@example.com")
    workspace_id = (await owner.post(WS, json={"name": "Shared"})).json()["id"]
    await _invite_and_accept(owner, editor, workspace_id, "editor-role@example.com", "editor")

    # Can read...
    assert (await editor.get(f"{WS}/{workspace_id}")).status_code == 200
    assert (await editor.get(f"{WS}/{workspace_id}/members")).status_code == 200

    # ...but not administer.
    assert (await editor.patch(f"{WS}/{workspace_id}", json={"name": "Renamed"})).status_code == 403
    assert (await editor.delete(f"{WS}/{workspace_id}")).status_code == 403
    assert (
        await editor.post(f"{WS}/{workspace_id}/invitations", json={"email": "z@example.com"})
    ).status_code == 403


async def test_viewer_cannot_administer(make_client):
    owner, _ = await new_user(make_client, "owner-v@example.com")
    viewer, _ = await new_user(make_client, "viewer-v@example.com")
    workspace_id = (await owner.post(WS, json={"name": "Viewing"})).json()["id"]
    await _invite_and_accept(owner, viewer, workspace_id, "viewer-v@example.com", "viewer")

    assert (await viewer.get(f"{WS}/{workspace_id}")).status_code == 200
    assert (await viewer.patch(f"{WS}/{workspace_id}", json={"name": "No"})).status_code == 403


# --- Last-owner invariant (adversarial) ------------------------------------------

async def test_sole_owner_cannot_be_demoted(make_client):
    """A workspace with no owner can never be administered again."""
    owner, _ = await new_user(make_client, "sole-demote@example.com")
    workspace_id = (await owner.post(WS, json={"name": "Solo"})).json()["id"]
    members = (await owner.get(f"{WS}/{workspace_id}/members")).json()
    member_id = members[0]["id"]

    response = await owner.patch(
        f"{WS}/{workspace_id}/members/{member_id}", json={"role": "viewer"}
    )
    assert response.status_code == 409
    assert "only owner" in response.json()["detail"].lower()

    # Still an owner afterwards — the rejection was not cosmetic.
    assert (await owner.get(f"{WS}/{workspace_id}")).json()["role"] == "owner"


async def test_sole_owner_cannot_be_removed(make_client):
    owner, _ = await new_user(make_client, "sole-remove@example.com")
    workspace_id = (await owner.post(WS, json={"name": "Solo2"})).json()["id"]
    member_id = (await owner.get(f"{WS}/{workspace_id}/members")).json()[0]["id"]

    response = await owner.delete(f"{WS}/{workspace_id}/members/{member_id}")
    assert response.status_code == 409
    assert (await owner.get(f"{WS}/{workspace_id}/members")).status_code == 200


async def test_owner_can_be_demoted_once_another_owner_exists(make_client):
    first, _ = await new_user(make_client, "first-owner@example.com")
    second, _ = await new_user(make_client, "second-owner@example.com")
    workspace_id = (await first.post(WS, json={"name": "Two Owners"})).json()["id"]
    await _invite_and_accept(first, second, workspace_id, "second-owner@example.com", "editor")

    members = (await first.get(f"{WS}/{workspace_id}/members")).json()
    second_member = next(m for m in members if m["email"] == "second-owner@example.com")
    first_member = next(m for m in members if m["email"] == "first-owner@example.com")

    promote = await first.patch(
        f"{WS}/{workspace_id}/members/{second_member['id']}", json={"role": "owner"}
    )
    assert promote.status_code == 200

    # Now the original owner may step down.
    demote = await first.patch(
        f"{WS}/{workspace_id}/members/{first_member['id']}", json={"role": "viewer"}
    )
    assert demote.status_code == 200
    assert (await first.get(f"{WS}/{workspace_id}")).json()["role"] == "viewer"


async def test_owner_pointer_follows_ownership(make_client, db):
    """workspaces.owner_id must never point at someone who is no longer an owner."""
    first, _ = await new_user(make_client, "ptr-first@example.com")
    second, second_session = await new_user(make_client, "ptr-second@example.com")
    workspace_id = (await first.post(WS, json={"name": "Pointer"})).json()["id"]
    await _invite_and_accept(first, second, workspace_id, "ptr-second@example.com", "editor")

    members = (await first.get(f"{WS}/{workspace_id}/members")).json()
    second_member = next(m for m in members if m["email"] == "ptr-second@example.com")
    first_member = next(m for m in members if m["email"] == "ptr-first@example.com")

    await first.patch(f"{WS}/{workspace_id}/members/{second_member['id']}", json={"role": "owner"})
    await first.patch(f"{WS}/{workspace_id}/members/{first_member['id']}", json={"role": "viewer"})

    workspace = (await second.get(f"{WS}/{workspace_id}")).json()
    assert workspace["owner_id"] == second_session["user"]["id"]


# --- Personal workspace protections ----------------------------------------------

async def test_personal_workspace_cannot_be_deleted(make_client):
    user, session = await new_user(make_client, "personal-del@example.com")
    personal_id = session["workspaces"][0]["id"]

    response = await user.delete(f"{WS}/{personal_id}")
    assert response.status_code == 409
    assert (await user.get(f"{WS}/{personal_id}")).status_code == 200


async def test_non_personal_workspace_can_be_deleted(make_client):
    user, _ = await new_user(make_client, "team-del@example.com")
    workspace_id = (await user.post(WS, json={"name": "Disposable"})).json()["id"]

    assert (await user.delete(f"{WS}/{workspace_id}")).status_code == 204
    assert (await user.get(f"{WS}/{workspace_id}")).status_code == 404


# --- Cross-workspace object references (adversarial) -----------------------------

async def test_member_id_from_another_workspace_is_rejected(make_client):
    """Adversarial: owning workspace A must not let you edit a member row in B.

    Without an explicit scope check, `GET member by id` would happily load a row that
    belongs to a workspace the caller has nothing to do with.
    """
    alice, _ = await new_user(make_client, "a-scope@example.com")
    bob, bob_session = await new_user(make_client, "b-scope@example.com")

    alice_ws = (await alice.post(WS, json={"name": "Alice WS"})).json()["id"]
    bob_ws = bob_session["workspaces"][0]["id"]
    bob_member_id = (await bob.get(f"{WS}/{bob_ws}/members")).json()[0]["id"]

    response = await alice.patch(
        f"{WS}/{alice_ws}/members/{bob_member_id}", json={"role": "viewer"}
    )
    assert response.status_code == 404

    # Bob's role is untouched.
    assert (await bob.get(f"{WS}/{bob_ws}")).json()["role"] == "owner"
