"""M2 file, version and quota tests (Schema.md §6-7, Architecture.md §2, §3, §6)."""

from __future__ import annotations

from app.models.content import File, FileVersion
from app.models.enums import ScanStatus
from app.services import files as file_service
from app.services import quota
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str, quota_bytes: int | None = None):
    client, _ = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Files"})).json()["id"]
    if quota_bytes is not None:
        await client.patch(f"{WS}/{ws}", json={"storage_quota_bytes": quota_bytes})
    return client, ws


# --- Upload ----------------------------------------------------------------------

async def test_upload_creates_a_file_and_first_version(make_client):
    client, ws = await _workspace(make_client, "f-up@example.com")
    response = await upload_file(client, ws, "report.pdf", size_bytes=4096)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "report.pdf"
    assert body["size_bytes"] == 4096

    versions = (await client.get(f"{WS}/{ws}/files/{body['id']}/versions")).json()
    assert len(versions) == 1
    assert versions[0]["version_number"] == 1
    assert versions[0]["is_current"] is True


async def test_upload_init_returns_presigned_parts(make_client):
    client, ws = await _workspace(make_client, "f-parts@example.com")
    init = await client.post(
        f"{WS}/{ws}/uploads/init",
        json={"name": "big.bin", "size_bytes": 20 * 1024 * 1024},
    )
    assert init.status_code == 201
    parts = init.json()["parts"]
    # 20MB at an 8MB chunk size: 8 + 8 + 4.
    assert [p["size_bytes"] for p in parts] == [8388608, 8388608, 4194304]
    assert all(p["url"].startswith("https://") for p in parts)


async def test_reupload_same_name_creates_a_new_version(make_client):
    """Schema.md §6: this is what the unique index makes well-defined."""
    client, ws = await _workspace(make_client, "f-rev@example.com")
    first = (await upload_file(client, ws, "notes.txt", size_bytes=100)).json()
    second = (await upload_file(client, ws, "notes.txt", size_bytes=250)).json()

    assert first["id"] == second["id"], "a re-upload must not create a second file"
    assert second["size_bytes"] == 250

    versions = (await client.get(f"{WS}/{ws}/files/{first['id']}/versions")).json()
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["is_current"] is True

    listing = (await client.get(f"{WS}/{ws}/folders")).json()
    assert len(listing["files"]) == 1


async def test_version_history_is_capped_and_prunes_oldest(make_client, db):
    """Schema.md §7: without a cap one file can exhaust a workspace."""
    client, ws = await _workspace(make_client, "f-prune@example.com")
    for _ in range(file_service.MAX_RETAINED_VERSIONS + 3):
        response = await upload_file(client, ws, "churn.bin", size_bytes=1000)
        assert response.status_code == 200, response.text

    file_id = response.json()["id"]
    versions = (await client.get(f"{WS}/{ws}/files/{file_id}/versions")).json()
    assert len(versions) == file_service.MAX_RETAINED_VERSIONS
    # The oldest went, the newest stayed.
    numbers = [v["version_number"] for v in versions]
    assert max(numbers) == file_service.MAX_RETAINED_VERSIONS + 3
    assert min(numbers) == 4


# --- Quota -----------------------------------------------------------------------

async def test_quota_counts_every_retained_version(make_client, db):
    """Architecture.md §6. Counting current versions only would make the
    reconciliation task report drift forever."""
    client, ws = await _workspace(make_client, "f-quota-versions@example.com")
    await upload_file(client, ws, "doc.txt", size_bytes=1000)
    await upload_file(client, ws, "doc.txt", size_bytes=1000)
    await upload_file(client, ws, "doc.txt", size_bytes=1000)

    used = (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"]
    assert used == 3000, "three versions of 1000 bytes must consume 3000"


async def test_quota_blocks_an_upload_that_would_exceed_it(make_client):
    client, ws = await _workspace(make_client, "f-quota-block@example.com", quota_bytes=5000)
    assert (await upload_file(client, ws, "a.bin", size_bytes=4000)).status_code == 200

    over = await client.post(
        f"{WS}/{ws}/uploads/init", json={"name": "b.bin", "size_bytes": 2000}
    )
    assert over.status_code == 413
    # And nothing was reserved by the failed attempt.
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 4000


async def test_provider_file_size_ceiling_is_enforced(make_client):
    """A file above the provider's per-file limit is refused before any storage call,
    not part-way through the upload."""
    client, ws = await _workspace(
        make_client, "f-toobig@example.com", quota_bytes=10 * 1024 * 1024 * 1024
    )
    response = await client.post(
        f"{WS}/{ws}/uploads/init",
        json={"name": "huge.bin", "size_bytes": 100 * 1024 * 1024},
    )
    assert response.status_code == 413
    assert "limit" in response.json()["detail"].lower()


async def test_deleting_a_file_returns_its_space(make_client):
    client, ws = await _workspace(make_client, "f-quota-del@example.com")
    file_id = (await upload_file(client, ws, "big.bin", size_bytes=5000)).json()["id"]
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 5000

    await client.delete(f"{WS}/{ws}/files/{file_id}")
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 0


async def test_reconcile_corrects_a_drifted_counter(make_client, db):
    """The safety net in Architecture.md §6 — proven to actually correct, not just run."""
    client, ws = await _workspace(make_client, "f-recon@example.com")
    await upload_file(client, ws, "a.bin", size_bytes=2000)

    import uuid as _uuid

    from app.models.identity import Workspace

    workspace = await db.get(Workspace, _uuid.UUID(ws))
    workspace.storage_used_bytes = 999999  # simulate drift
    await db.flush()

    report = await quota.reconcile(db, _uuid.UUID(ws))
    assert report["actual"] == 2000
    assert report["drift"] == 999999 - 2000
    assert report["stored"] == 2000


async def test_pruned_versions_release_their_space(make_client):
    client, ws = await _workspace(make_client, "f-prune-quota@example.com")
    for _ in range(file_service.MAX_RETAINED_VERSIONS + 2):
        await upload_file(client, ws, "churn.bin", size_bytes=1000)

    used = (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"]
    assert used == file_service.MAX_RETAINED_VERSIONS * 1000


# --- Scan status gating (Architecture.md §3) --------------------------------------

async def test_uploads_are_marked_not_scanned_when_scanning_is_off(make_client):
    """The badge must tell the truth. Marking an unscanned file `clean` would make the
    platform's core trust signal lie for two milestones (Design.md §5)."""
    client, ws = await _workspace(make_client, "f-scan@example.com")
    body = (await upload_file(client, ws, "doc.txt")).json()
    assert body["scan_status"] == ScanStatus.SCAN_SKIPPED.value


async def test_a_flagged_file_is_never_downloadable(make_client, db):
    """Adversarial: not even to its owner (Architecture.md §3).

    The status is set on the **version**, which is where M4 moved the access decision.
    `files.scan_status` is a mirror of the current version for listings; deciding on the
    mirror would mean an override recorded on the version could not be seen by the code
    granting access.
    """
    client, ws = await _workspace(make_client, "f-flag@example.com")
    file_id = (await upload_file(client, ws, "virus.exe")).json()["id"]

    import uuid as _uuid

    file = await db.get(File, _uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    file.scan_status = ScanStatus.FLAGGED
    await db.flush()

    response = await client.get(f"{WS}/{ws}/files/{file_id}/download")
    assert response.status_code == 403
    assert "flagged" in response.json()["detail"].lower()


async def test_a_pending_file_is_not_downloadable(make_client, db):
    client, ws = await _workspace(make_client, "f-pending@example.com")
    file_id = (await upload_file(client, ws, "waiting.txt")).json()["id"]

    import uuid as _uuid

    file = await db.get(File, _uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.PENDING
    file.scan_status = ScanStatus.PENDING
    await db.flush()

    assert (await client.get(f"{WS}/{ws}/files/{file_id}/download")).status_code == 403


async def test_download_returns_a_signed_url_not_bytes(make_client):
    """Rules.md §3: the API never proxies file bytes."""
    client, ws = await _workspace(make_client, "f-dl@example.com")
    file_id = (await upload_file(client, ws, "ok.txt")).json()["id"]

    response = await client.get(f"{WS}/{ws}/files/{file_id}/download")
    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith("https://")
    assert body["expires_in_seconds"] <= 900


# --- Move, rename, delete, restore ------------------------------------------------

async def test_move_file_between_folders(make_client):
    client, ws = await _workspace(make_client, "f-move@example.com")
    a = (await client.post(f"{WS}/{ws}/folders", json={"name": "A"})).json()["id"]
    b = (await client.post(f"{WS}/{ws}/folders", json={"name": "B"})).json()["id"]
    file_id = (await upload_file(client, ws, "doc.txt", folder_id=a)).json()["id"]

    response = await client.patch(
        f"{WS}/{ws}/files/{file_id}", json={"move": True, "folder_id": b}
    )
    assert response.status_code == 200
    assert (await client.get(f"{WS}/{ws}/folders/{a}")).json()["files"] == []
    assert len((await client.get(f"{WS}/{ws}/folders/{b}")).json()["files"]) == 1


async def test_name_collision_on_move_is_refused(make_client):
    client, ws = await _workspace(make_client, "f-collide@example.com")
    a = (await client.post(f"{WS}/{ws}/folders", json={"name": "A"})).json()["id"]
    b = (await client.post(f"{WS}/{ws}/folders", json={"name": "B"})).json()["id"]
    file_id = (await upload_file(client, ws, "same.txt", folder_id=a)).json()["id"]
    await upload_file(client, ws, "same.txt", folder_id=b)

    response = await client.patch(
        f"{WS}/{ws}/files/{file_id}", json={"move": True, "folder_id": b}
    )
    assert response.status_code == 409


async def test_delete_then_restore_a_file(make_client):
    client, ws = await _workspace(make_client, "f-restore@example.com")
    file_id = (await upload_file(client, ws, "oops.txt", size_bytes=700)).json()["id"]

    receipt = (await client.delete(f"{WS}/{ws}/files/{file_id}")).json()
    assert (await client.get(f"{WS}/{ws}/files/{file_id}")).status_code == 404

    restored = await client.post(
        f"{WS}/{ws}/files/restore",
        json={"deletion_batch_id": receipt["deletion_batch_id"]},
    )
    assert restored.status_code == 200
    assert (await client.get(f"{WS}/{ws}/files/{file_id}")).status_code == 200
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 700


async def test_tags_can_be_set_and_are_normalised(make_client):
    client, ws = await _workspace(make_client, "f-tags@example.com")
    file_id = (await upload_file(client, ws, "tagged.txt")).json()["id"]
    response = await client.patch(
        f"{WS}/{ws}/files/{file_id}", json={"tags": ["  Finance ", "Q3", "finance"]}
    )
    assert response.status_code == 200
    assert response.json()["tags"] == ["finance", "q3"]


# --- Adversarial ------------------------------------------------------------------

async def test_file_from_another_workspace_is_invisible(make_client):
    alice, alice_ws = await _workspace(make_client, "f-alice@example.com")
    bob, bob_ws = await _workspace(make_client, "f-bob@example.com")
    file_id = (await upload_file(alice, alice_ws, "secret.txt")).json()["id"]

    assert (await bob.get(f"{WS}/{bob_ws}/files/{file_id}")).status_code == 404
    assert (await bob.get(f"{WS}/{bob_ws}/files/{file_id}/download")).status_code == 404
    assert (await bob.delete(f"{WS}/{bob_ws}/files/{file_id}")).status_code == 404
    assert (await bob.get(f"{WS}/{alice_ws}/files/{file_id}")).status_code == 404
    assert (await alice.get(f"{WS}/{alice_ws}/files/{file_id}")).status_code == 200


async def test_completing_an_upload_twice_is_refused(make_client):
    """Adversarial: replaying upload-complete must not double-count quota."""
    client, ws = await _workspace(make_client, "f-double@example.com")
    init = await client.post(
        f"{WS}/{ws}/uploads/init", json={"name": "once.txt", "size_bytes": 500}
    )
    payload = init.json()
    parts = [{"part_number": p["part_number"], "etag": '"e"'} for p in payload["parts"]]
    body = {"version_id": payload["version_id"], "parts": parts}

    assert (await client.post(f"{WS}/{ws}/uploads/complete", json=body)).status_code == 200
    second = await client.post(f"{WS}/{ws}/uploads/complete", json=body)
    assert second.status_code == 409
    assert (await client.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"] == 500


async def test_upload_init_into_another_workspaces_folder_is_refused(make_client):
    alice, alice_ws = await _workspace(make_client, "f-a2@example.com")
    bob, bob_ws = await _workspace(make_client, "f-b2@example.com")
    alice_folder = (
        await alice.post(f"{WS}/{alice_ws}/folders", json={"name": "Private"})
    ).json()["id"]

    response = await bob.post(
        f"{WS}/{bob_ws}/uploads/init",
        json={"name": "sneak.txt", "size_bytes": 10, "folder_id": alice_folder},
    )
    assert response.status_code == 404
