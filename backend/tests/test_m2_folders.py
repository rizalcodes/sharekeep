"""M2 folder tree tests (Schema.md §5, Architecture.md §4.1, §6a)."""

from __future__ import annotations

import uuid

from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str):
    client, session = await new_user(make_client, email)
    workspace_id = (await client.post(WS, json={"name": "Files"})).json()["id"]
    return client, workspace_id


async def _mkdir(client, workspace_id, name, parent=None):
    body = {"name": name}
    if parent is not None:
        body["parent_folder_id"] = str(parent)
    response = await client.post(f"{WS}/{workspace_id}/folders", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- Basics ----------------------------------------------------------------------

async def test_create_and_list_root(make_client):
    client, ws = await _workspace(make_client, "tree-root@example.com")
    await _mkdir(client, ws, "Reports")

    listing = (await client.get(f"{WS}/{ws}/folders")).json()
    assert listing["folder"] is None
    assert [f["name"] for f in listing["folders"]] == ["Reports"]
    assert listing["breadcrumb"] == []


async def test_nested_folders_and_breadcrumb(make_client):
    client, ws = await _workspace(make_client, "tree-nest@example.com")
    a = await _mkdir(client, ws, "A")
    b = await _mkdir(client, ws, "B", parent=a)
    c = await _mkdir(client, ws, "C", parent=b)

    listing = (await client.get(f"{WS}/{ws}/folders/{c}")).json()
    assert [entry["name"] for entry in listing["breadcrumb"]] == ["A", "B", "C"]


async def test_duplicate_sibling_name_is_refused(make_client):
    client, ws = await _workspace(make_client, "tree-dupe@example.com")
    await _mkdir(client, ws, "Reports")
    again = await client.post(f"{WS}/{ws}/folders", json={"name": "Reports"})
    assert again.status_code == 409


async def test_same_name_allowed_in_different_parents(make_client):
    client, ws = await _workspace(make_client, "tree-same@example.com")
    a = await _mkdir(client, ws, "A")
    b = await _mkdir(client, ws, "B")
    await _mkdir(client, ws, "Shared", parent=a)
    await _mkdir(client, ws, "Shared", parent=b)  # must not raise


async def test_two_root_folders_of_the_same_name_are_refused(make_client):
    """NULLS NOT DISTINCT: without it Postgres treats each NULL parent as unique and
    a workspace could hold unlimited root folders with identical names."""
    client, ws = await _workspace(make_client, "tree-nulls@example.com")
    await _mkdir(client, ws, "Root Level")
    again = await client.post(f"{WS}/{ws}/folders", json={"name": "Root Level"})
    assert again.status_code == 409


async def test_slashes_in_names_are_rejected(make_client):
    client, ws = await _workspace(make_client, "tree-slash@example.com")
    for bad in ("a/b", "a\\b", "..", "."):
        response = await client.post(f"{WS}/{ws}/folders", json={"name": bad})
        assert response.status_code == 422, bad


# --- Move, cycles, depth (adversarial) -------------------------------------------

async def test_move_folder_into_its_own_descendant_is_refused(make_client):
    """Adversarial: a cycle makes the ancestor walk that every permission check
    depends on non-terminating, so this guard protects the security path."""
    client, ws = await _workspace(make_client, "tree-cycle@example.com")
    a = await _mkdir(client, ws, "A")
    b = await _mkdir(client, ws, "B", parent=a)
    c = await _mkdir(client, ws, "C", parent=b)

    response = await client.patch(
        f"{WS}/{ws}/folders/{a}", json={"move": True, "parent_folder_id": c}
    )
    assert response.status_code == 409
    assert "descendant" in response.json()["detail"].lower()

    # The tree is unchanged and still traversable.
    listing = (await client.get(f"{WS}/{ws}/folders/{c}")).json()
    assert [e["name"] for e in listing["breadcrumb"]] == ["A", "B", "C"]


async def test_move_folder_into_itself_is_refused(make_client):
    client, ws = await _workspace(make_client, "tree-self@example.com")
    a = await _mkdir(client, ws, "A")
    response = await client.patch(
        f"{WS}/{ws}/folders/{a}", json={"move": True, "parent_folder_id": a}
    )
    assert response.status_code == 409


async def test_nesting_depth_is_capped(make_client):
    """Every object permission check walks this chain; unbounded depth is unbounded
    cost on the hottest path in the system."""
    client, ws = await _workspace(make_client, "tree-depth@example.com")
    parent = None
    for level in range(32):
        body = {"name": f"L{level}"}
        if parent is not None:
            body["parent_folder_id"] = str(parent)
        response = await client.post(f"{WS}/{ws}/folders", json=body)
        if response.status_code == 409:
            assert level >= 30, f"cap fired too early at level {level}"
            return
        assert response.status_code == 201, response.text
        parent = response.json()["id"]

    # If 32 levels all succeeded, the 33rd must not.
    response = await client.post(
        f"{WS}/{ws}/folders", json={"name": "TooDeep", "parent_folder_id": str(parent)}
    )
    assert response.status_code == 409


async def test_move_to_root(make_client):
    client, ws = await _workspace(make_client, "tree-toroot@example.com")
    a = await _mkdir(client, ws, "A")
    b = await _mkdir(client, ws, "B", parent=a)

    response = await client.patch(
        f"{WS}/{ws}/folders/{b}", json={"move": True, "parent_folder_id": None}
    )
    assert response.status_code == 200
    assert response.json()["parent_folder_id"] is None


# --- Delete and restore ----------------------------------------------------------

async def test_deleting_a_folder_cascades_to_descendants_and_files(make_client):
    client, ws = await _workspace(make_client, "tree-del@example.com")
    a = await _mkdir(client, ws, "A")
    b = await _mkdir(client, ws, "B", parent=a)
    await upload_file(client, ws, "inside.txt", size_bytes=2048, folder_id=b)

    response = await client.delete(f"{WS}/{ws}/folders/{a}")
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["items_deleted"] == 2
    assert receipt["bytes_released"] == 2048

    assert (await client.get(f"{WS}/{ws}/folders/{a}")).status_code == 404
    assert (await client.get(f"{WS}/{ws}/folders/{b}")).status_code == 404
    assert (await client.get(f"{WS}/{ws}/folders")).json()["folders"] == []


async def test_restore_revives_the_batch(make_client):
    client, ws = await _workspace(make_client, "tree-restore@example.com")
    a = await _mkdir(client, ws, "A")
    await _mkdir(client, ws, "B", parent=a)

    batch = (await client.delete(f"{WS}/{ws}/folders/{a}")).json()["deletion_batch_id"]
    response = await client.post(
        f"{WS}/{ws}/folders/restore", json={"deletion_batch_id": batch}
    )
    assert response.status_code == 200
    assert response.json()["restored"] == 2
    assert (await client.get(f"{WS}/{ws}/folders/{a}")).status_code == 200


async def test_restore_does_not_resurrect_earlier_separate_deletions(make_client):
    """The whole reason deletion_batch_id exists (Architecture.md §6a).

    A file the user deleted on purpose must stay deleted when the folder that
    contained it is later deleted and restored.
    """
    client, ws = await _workspace(make_client, "tree-batch@example.com")
    folder = await _mkdir(client, ws, "Archive")
    await upload_file(client, ws, "keep.txt", folder_id=folder)
    await upload_file(client, ws, "throwaway.txt", folder_id=folder)

    listing = (await client.get(f"{WS}/{ws}/folders/{folder}")).json()
    throwaway = next(f for f in listing["files"] if f["name"] == "throwaway.txt")

    # Deleted deliberately, first and separately.
    await client.delete(f"{WS}/{ws}/files/{throwaway['id']}")

    # Then the whole folder goes, and comes back.
    batch = (await client.delete(f"{WS}/{ws}/folders/{folder}")).json()["deletion_batch_id"]
    await client.post(f"{WS}/{ws}/folders/restore", json={"deletion_batch_id": batch})

    listing = (await client.get(f"{WS}/{ws}/folders/{folder}")).json()
    names = {f["name"] for f in listing["files"]}
    assert "keep.txt" in names
    assert "throwaway.txt" not in names, "restore resurrected a deliberately deleted file"


async def test_restoring_an_unknown_batch_is_refused(make_client):
    client, ws = await _workspace(make_client, "tree-nobatch@example.com")
    response = await client.post(
        f"{WS}/{ws}/folders/restore", json={"deletion_batch_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


# --- Cross-workspace isolation (adversarial) -------------------------------------

async def test_folder_from_another_workspace_is_invisible(make_client):
    alice, alice_ws = await _workspace(make_client, "tree-alice@example.com")
    bob, bob_ws = await _workspace(make_client, "tree-bob@example.com")
    alice_folder = await _mkdir(alice, alice_ws, "Private")

    # Through Bob's own workspace, using Alice's folder id.
    assert (await bob.get(f"{WS}/{bob_ws}/folders/{alice_folder}")).status_code == 404
    assert (
        await bob.patch(f"{WS}/{bob_ws}/folders/{alice_folder}", json={"name": "Owned"})
    ).status_code == 404
    assert (await bob.delete(f"{WS}/{bob_ws}/folders/{alice_folder}")).status_code == 404
    # And through Alice's workspace id, which Bob is not a member of.
    assert (await bob.get(f"{WS}/{alice_ws}/folders/{alice_folder}")).status_code == 404

    assert (await alice.get(f"{WS}/{alice_ws}/folders/{alice_folder}")).status_code == 200
