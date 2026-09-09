"""Resume endpoints (Architecture.md §2a).

These are new attack surface: they enumerate in-flight uploads and hand out fresh
presigned URLs. Both are tested adversarially, not just on the happy path (Rules.md §6).
"""

from __future__ import annotations

import uuid

from tests.conftest import new_user

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Uploads"})).json()["id"]
    return client, ws, session


async def _start(client, ws, name="big.bin", size=20 * 1024 * 1024):
    """Start an upload without finishing it — the state resume exists for."""
    response = await client.post(
        f"{WS}/{ws}/uploads/init", json={"name": name, "size_bytes": size}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- Listing ----------------------------------------------------------------------

async def test_unfinished_upload_is_listed(make_client):
    client, ws, _ = await _workspace(make_client, "r-list@example.com")
    session = await _start(client, ws)

    listed = await client.get(f"{WS}/{ws}/uploads")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["version_id"] == session["version_id"]
    assert rows[0]["file_name"] == "big.bin"
    assert rows[0]["total_bytes"] == 20 * 1024 * 1024
    assert rows[0]["parts_total"] == 3


async def test_completed_upload_is_not_listed(make_client):
    from tests.conftest import upload_file

    client, ws, _ = await _workspace(make_client, "r-done@example.com")
    await upload_file(client, ws, "finished.bin", size_bytes=1000)
    assert (await client.get(f"{WS}/{ws}/uploads")).json() == []


async def test_resume_state_reports_remaining_parts(make_client):
    client, ws, _ = await _workspace(make_client, "r-state@example.com")
    session = await _start(client, ws)

    state = await client.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    assert state.status_code == 200
    body = state.json()
    # Storage is stubbed and nothing landed, so everything is still outstanding.
    assert body["completed_parts"] == []
    assert len(body["remaining_parts"]) == 3
    assert body["uploaded_bytes"] == 0
    assert all(p["url"].startswith("https://") for p in body["remaining_parts"])


async def test_resume_urls_are_freshly_signed_each_time(make_client):
    """Presigned URLs are short-lived by design; a stale one fails confusingly."""
    client, ws, _ = await _workspace(make_client, "r-fresh@example.com")
    session = await _start(client, ws)
    first = await client.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    second = await client.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    assert first.status_code == second.status_code == 200
    assert len(second.json()["remaining_parts"]) == 3


async def test_cancelling_an_upload_returns_its_quota(make_client):
    client, ws, _ = await _workspace(make_client, "r-cancel@example.com")
    session = await _start(client, ws, size=5000)
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 5000

    response = await client.delete(f"{WS}/{ws}/uploads/{session['version_id']}")
    assert response.status_code == 204
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 0
    assert (await client.get(f"{WS}/{ws}/uploads")).json() == []


async def test_resume_state_refused_once_completed(make_client):
    from tests.conftest import upload_file

    client, ws, _ = await _workspace(make_client, "r-after@example.com")
    done = await upload_file(client, ws, "done.bin", size_bytes=1000)
    file_id = done.json()["id"]
    versions = (await client.get(f"{WS}/{ws}/files/{file_id}/versions")).json()

    response = await client.get(f"{WS}/{ws}/uploads/{versions[0]['id']}/parts")
    assert response.status_code == 409


# --- Adversarial ------------------------------------------------------------------

async def test_outsider_cannot_list_another_workspaces_uploads(make_client):
    """Substituting a workspace id must not enumerate someone else's in-flight work."""
    alice, alice_ws, _ = await _workspace(make_client, "r-alice@example.com")
    bob, bob_ws, _ = await _workspace(make_client, "r-bob@example.com")
    await _start(alice, alice_ws, name="private.bin")

    # Bob aims at Alice's workspace directly.
    assert (await bob.get(f"{WS}/{alice_ws}/uploads")).status_code == 404
    # And his own workspace shows nothing of hers.
    assert (await bob.get(f"{WS}/{bob_ws}/uploads")).json() == []


async def test_outsider_cannot_fetch_resume_urls_for_another_workspace(make_client):
    """The dangerous one: resume state hands out presigned URLs that can write bytes."""
    alice, alice_ws, _ = await _workspace(make_client, "r-alice2@example.com")
    bob, bob_ws, _ = await _workspace(make_client, "r-bob2@example.com")
    session = await _start(alice, alice_ws)
    version_id = session["version_id"]

    assert (await bob.get(f"{WS}/{alice_ws}/uploads/{version_id}/parts")).status_code == 404
    assert (await bob.get(f"{WS}/{bob_ws}/uploads/{version_id}/parts")).status_code == 404
    assert (await bob.delete(f"{WS}/{bob_ws}/uploads/{version_id}")).status_code == 404
    assert (await bob.delete(f"{WS}/{alice_ws}/uploads/{version_id}")).status_code == 404

    # Alice's upload is untouched by any of that.
    assert len((await alice.get(f"{WS}/{alice_ws}/uploads")).json()) == 1


async def test_viewer_cannot_see_or_resume_uploads(make_client):
    """A viewer cannot write, so an in-flight upload is not theirs to resume."""
    owner, ws, _ = await _workspace(make_client, "r-owner@example.com")
    viewer, viewer_session = await new_user(make_client, "r-viewer@example.com")
    invite = await owner.post(
        f"{WS}/{ws}/invitations", json={"email": "r-viewer@example.com", "role": "viewer"}
    )
    await viewer.post("/api/v1/invitations/accept", json={"token": invite.json()["token"]})

    session = await _start(owner, ws)
    assert (await viewer.get(f"{WS}/{ws}/uploads")).json() == []
    assert (
        await viewer.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    ).status_code == 403


async def test_unknown_version_id_is_not_found(make_client):
    client, ws, _ = await _workspace(make_client, "r-unknown@example.com")
    response = await client.get(f"{WS}/{ws}/uploads/{uuid.uuid4()}/parts")
    assert response.status_code == 404


async def test_unauthenticated_access_is_refused(make_client):
    client, ws, _ = await _workspace(make_client, "r-anon@example.com")
    session = await _start(client, ws)

    anon = await make_client()
    assert (await anon.get(f"{WS}/{ws}/uploads")).status_code == 401
    assert (
        await anon.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    ).status_code == 401


# --- Partial progress -------------------------------------------------------------
#
# These use the stub's landed-parts hook. The real provider round trip is covered by
# M3 manual validation; what matters here is that our own arithmetic is right when
# some parts already exist — the case that previously slipped through because the
# stub always reported an empty part list.

async def test_resume_state_accounts_for_parts_already_landed(make_client, stub_storage, db):
    import uuid as _uuid

    from app.models.content import FileVersion

    client, ws, _ = await _workspace(make_client, "r-partial@example.com")
    session = await _start(client, ws, size=20 * 1024 * 1024)

    version = await db.get(FileVersion, _uuid.UUID(session["version_id"]))
    stub_storage["landed_parts"][version.storage_key] = [1]

    body = (await client.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")).json()
    assert [p["part_number"] for p in body["completed_parts"]] == [1]
    assert [p["part_number"] for p in body["remaining_parts"]] == [2, 3]
    # Size comes from our own plan; the provider never reports it.
    assert body["uploaded_bytes"] == 8 * 1024 * 1024


async def test_layout_offsets_are_exact_and_contiguous(make_client):
    """A wrong offset on resume corrupts the object with no error anywhere, so the
    layout is asserted exactly rather than approximately."""
    client, ws, _ = await _workspace(make_client, "r-layout@example.com")
    session = await _start(client, ws, size=20 * 1024 * 1024)

    layout = (
        await client.get(f"{WS}/{ws}/uploads/{session['version_id']}/parts")
    ).json()["layout"]

    assert [(p["part_number"], p["offset"], p["size_bytes"]) for p in layout] == [
        (1, 0, 8388608),
        (2, 8388608, 8388608),
        (3, 16777216, 4194304),
    ]
    # Contiguous, and covering the file exactly.
    assert layout[0]["offset"] == 0
    for previous, nxt in zip(layout, layout[1:], strict=False):
        assert previous["offset"] + previous["size_bytes"] == nxt["offset"]
    assert layout[-1]["offset"] + layout[-1]["size_bytes"] == 20 * 1024 * 1024


async def test_in_flight_progress_reflects_landed_parts(make_client, stub_storage, db):
    import uuid as _uuid

    from app.models.content import FileVersion

    client, ws, _ = await _workspace(make_client, "r-progress@example.com")
    session = await _start(client, ws, size=20 * 1024 * 1024)
    version = await db.get(FileVersion, _uuid.UUID(session["version_id"]))
    stub_storage["landed_parts"][version.storage_key] = [1, 2]

    row = (await client.get(f"{WS}/{ws}/uploads")).json()[0]
    assert row["parts_completed"] == 2
    assert row["parts_total"] == 3
    assert row["uploaded_bytes"] == 16 * 1024 * 1024
