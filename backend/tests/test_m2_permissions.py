"""Object-level permission resolution (Architecture.md §4.1).

The rule under test that is easiest to get wrong: **nearest ancestor wins, including
when it is less permissive**. Most-permissive-wins would make a deliberately narrowing
grant silently ineffective — a security failure, not a UX quirk.
"""

from __future__ import annotations

from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _shared_workspace(make_client, owner_email: str, member_email: str, role="viewer"):
    """An owner and a second member in one workspace, via the real endpoints."""
    owner, _ = await new_user(make_client, owner_email)
    member, member_session = await new_user(make_client, member_email)
    ws = (await owner.post(WS, json={"name": "Shared"})).json()["id"]

    invite = await owner.post(
        f"{WS}/{ws}/invitations", json={"email": member_email, "role": role}
    )
    token = invite.json()["token"]
    accepted = await member.post("/api/v1/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    return owner, member, member_session["user"]["id"], ws


async def _mkdir(client, ws, name, parent=None):
    body = {"name": name}
    if parent is not None:
        body["parent_folder_id"] = str(parent)
    response = await client.post(f"{WS}/{ws}/folders", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- Workspace role is the fallback ----------------------------------------------

async def test_viewer_can_read_but_not_write(make_client):
    owner, member, _, ws = await _shared_workspace(
        make_client, "p-o1@example.com", "p-v1@example.com", role="viewer"
    )
    folder = await _mkdir(owner, ws, "Docs")

    assert (await member.get(f"{WS}/{ws}/folders/{folder}")).status_code == 200
    created = await member.post(
        f"{WS}/{ws}/folders", json={"name": "Nope", "parent_folder_id": folder}
    )
    assert created.status_code == 403


async def test_editor_can_write(make_client):
    owner, member, _, ws = await _shared_workspace(
        make_client, "p-o2@example.com", "p-e2@example.com", role="editor"
    )
    folder = await _mkdir(owner, ws, "Docs")
    created = await member.post(
        f"{WS}/{ws}/folders", json={"name": "Mine", "parent_folder_id": folder}
    )
    assert created.status_code == 201


# --- Grants widen ----------------------------------------------------------------

async def test_folder_grant_raises_a_viewer_to_editor_in_that_subtree(make_client):
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o3@example.com", "p-v3@example.com", role="viewer"
    )
    open_folder = await _mkdir(owner, ws, "Open")
    closed_folder = await _mkdir(owner, ws, "Closed")

    grant = await owner.post(
        f"{WS}/{ws}/folders/{open_folder}/grants",
        json={"user_id": member_id, "role": "editor"},
    )
    assert grant.status_code == 201

    # Editor inside the granted subtree...
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Sub", "parent_folder_id": open_folder}
        )
    ).status_code == 201
    # ...still only a viewer elsewhere.
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Sub", "parent_folder_id": closed_folder}
        )
    ).status_code == 403


async def test_grant_is_inherited_by_descendants(make_client):
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o4@example.com", "p-v4@example.com", role="viewer"
    )
    top = await _mkdir(owner, ws, "Top")
    middle = await _mkdir(owner, ws, "Middle", parent=top)
    bottom = await _mkdir(owner, ws, "Bottom", parent=middle)

    await owner.post(
        f"{WS}/{ws}/folders/{top}/grants", json={"user_id": member_id, "role": "editor"}
    )
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Deep", "parent_folder_id": bottom}
        )
    ).status_code == 201


# --- Grants narrow: the case most implementations get wrong -----------------------

async def test_nearest_ancestor_wins_even_when_less_permissive(make_client):
    """Adversarial: a narrowing grant on a subfolder must actually narrow.

    Under most-permissive-wins the editor grant on the parent would leak into the
    restricted child and the narrowing grant would be silently ineffective.
    """
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o5@example.com", "p-v5@example.com", role="viewer"
    )
    top = await _mkdir(owner, ws, "Team")
    restricted = await _mkdir(owner, ws, "Restricted", parent=top)

    await owner.post(
        f"{WS}/{ws}/folders/{top}/grants", json={"user_id": member_id, "role": "editor"}
    )
    await owner.post(
        f"{WS}/{ws}/folders/{restricted}/grants",
        json={"user_id": member_id, "role": "viewer"},
    )

    # Editor in the parent...
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "OK", "parent_folder_id": top}
        )
    ).status_code == 201
    # ...but the nearer viewer grant wins inside the restricted subtree.
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Blocked", "parent_folder_id": restricted}
        )
    ).status_code == 403


async def test_a_narrowing_grant_beats_a_higher_workspace_role(make_client):
    """An editor at workspace level, demoted to viewer on one subtree."""
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o6@example.com", "p-e6@example.com", role="editor"
    )
    sensitive = await _mkdir(owner, ws, "Sensitive")

    await owner.post(
        f"{WS}/{ws}/folders/{sensitive}/grants",
        json={"user_id": member_id, "role": "viewer"},
    )
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Nope", "parent_folder_id": sensitive}
        )
    ).status_code == 403


async def test_file_grant_beats_any_folder_grant(make_client):
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o7@example.com", "p-e7@example.com", role="editor"
    )
    folder = await _mkdir(owner, ws, "Docs")
    await upload_file(owner, ws, "locked.txt", folder_id=folder)
    listing = (await owner.get(f"{WS}/{ws}/folders/{folder}")).json()
    file_id = listing["files"][0]["id"]

    # Workspace editor can rename it today.
    assert (
        await member.patch(f"{WS}/{ws}/files/{file_id}", json={"name": "renamed.txt"})
    ).status_code == 200

    # A viewer grant on the file itself is the most specific rule and wins.
    await owner.post(
        f"{WS}/{ws}/files/{file_id}/grants", json={"user_id": member_id, "role": "viewer"}
    )
    assert (
        await member.patch(f"{WS}/{ws}/files/{file_id}", json={"name": "again.txt"})
    ).status_code == 403
    # Reading still works.
    assert (await member.get(f"{WS}/{ws}/files/{file_id}")).status_code == 200


async def test_revoking_a_grant_restores_the_workspace_role(make_client):
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o8@example.com", "p-e8@example.com", role="editor"
    )
    await upload_file(owner, ws, "doc.txt")
    file_id = (await owner.get(f"{WS}/{ws}/folders")).json()["files"][0]["id"]

    await owner.post(
        f"{WS}/{ws}/files/{file_id}/grants", json={"user_id": member_id, "role": "viewer"}
    )
    assert (
        await member.patch(f"{WS}/{ws}/files/{file_id}", json={"name": "x.txt"})
    ).status_code == 403

    assert (
        await owner.delete(f"{WS}/{ws}/files/{file_id}/grants/{member_id}")
    ).status_code == 204
    assert (
        await member.patch(f"{WS}/{ws}/files/{file_id}", json={"name": "x.txt"})
    ).status_code == 200


# --- Adversarial ------------------------------------------------------------------

async def test_editor_cannot_hand_out_grants(make_client):
    """Granting access is administrative; an editor must not be able to escalate."""
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o9@example.com", "p-e9@example.com", role="editor"
    )
    folder = await _mkdir(owner, ws, "Docs")
    response = await member.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": member_id, "role": "owner"},
    )
    assert response.status_code == 403


async def test_a_grant_does_not_leak_across_workspaces(make_client):
    """Adversarial: a grant is scoped to its object, never to the grantee generally."""
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o10@example.com", "p-v10@example.com", role="viewer"
    )
    other_ws = (await owner.post(WS, json={"name": "Other"})).json()["id"]
    folder = await _mkdir(owner, ws, "Granted")
    await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": member_id, "role": "owner"},
    )
    # Owner on one folder, still not a member of the other workspace at all.
    assert (await member.get(f"{WS}/{other_ws}/folders")).status_code == 404


async def test_grant_on_a_deleted_folder_confers_nothing(make_client):
    """Architecture.md §4.1: a grant whose target is soft-deleted must not apply."""
    owner, member, member_id, ws = await _shared_workspace(
        make_client, "p-o11@example.com", "p-v11@example.com", role="viewer"
    )
    parent = await _mkdir(owner, ws, "Parent")
    granted = await _mkdir(owner, ws, "Granted", parent=parent)
    await owner.post(
        f"{WS}/{ws}/folders/{granted}/grants",
        json={"user_id": member_id, "role": "editor"},
    )
    await owner.delete(f"{WS}/{ws}/folders/{granted}")

    # The subtree is gone, and the grant cannot be used to write into the parent.
    assert (
        await member.post(
            f"{WS}/{ws}/folders", json={"name": "Sneak", "parent_folder_id": parent}
        )
    ).status_code == 403
