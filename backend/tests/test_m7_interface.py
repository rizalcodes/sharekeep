"""M7a: the backend half of completing the interface.

Covers what M7 added or corrected under the API: member-only grants, listing and
revoking grants (folder grants could not be revoked at all), downloading an earlier
version, rollback, the trash and its retention sweep, and the permission indicator.
Written adversarially where access is involved, per Rules §6.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.content import File, FilePermission, FileVersion, ShareLink
from app.models.enums import AuditAction, ScanStatus, WorkspaceRole
from app.models.identity import AuditLog, Workspace, WorkspaceMember
from app.services import permissions
from app.tasks import maintenance_tasks
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str, name: str = "Interface"):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": name})).json()["id"]
    return client, ws, session


async def _member(db, ws: str, session: dict, role: WorkspaceRole) -> None:
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(session["user"]["id"]),
            role=role,
        )
    )
    await db.commit()


async def _audits(db, ws: str, action: AuditAction) -> list[AuditLog]:
    rows = await db.execute(
        select(AuditLog).where(
            AuditLog.workspace_id == uuid.UUID(ws), AuditLog.action == action
        )
    )
    return list(rows.scalars().all())


# --- Grants are member-only, at both layers (Architecture §4.1) ------------------


async def test_a_file_cannot_be_granted_to_a_non_member(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-g1@example.com")
    _, outsider = await new_user(make_client, "m7-g1-out@example.com")
    file_id = (await upload_file(owner, ws, "a.txt")).json()["id"]

    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/grants",
        json={"user_id": outsider["user"]["id"], "role": "viewer"},
    )
    assert refused.status_code == 422
    assert "Invite them" in refused.json()["detail"]


async def test_a_folder_cannot_be_granted_to_a_non_member(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-g2@example.com")
    _, outsider = await new_user(make_client, "m7-g2-out@example.com")
    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "F"})).json()["id"]

    refused = await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": outsider["user"]["id"], "role": "editor"},
    )
    assert refused.status_code == 422


async def test_a_grant_row_for_a_non_member_confers_nothing(make_client, db):
    """The second layer. Even a grant that exists in the table — written before M7, or
    by any future path that forgets the route check — must not admit a non-member."""
    owner, ws, _ = await _workspace(make_client, "m7-g3@example.com")
    outsider_client, outsider = await new_user(make_client, "m7-g3-out@example.com")
    file_id = uuid.UUID((await upload_file(owner, ws, "secret.txt")).json()["id"])

    db.add(
        FilePermission(
            file_id=file_id,
            user_id=uuid.UUID(outsider["user"]["id"]),
            role=WorkspaceRole.OWNER,
            granted_by=uuid.UUID((await owner.get("/api/v1/auth/me")).json()["user"]["id"]),
        )
    )
    await db.commit()

    from app.models.identity import User

    user = await db.get(User, uuid.UUID(outsider["user"]["id"]))
    role = await permissions.resolve_object_role(
        db, workspace_id=uuid.UUID(ws), user=user, file_id=file_id
    )
    assert role is None

    # And through the API, the file is as invisible as before.
    assert (await outsider_client.get(f"{WS}/{ws}/files/{file_id}")).status_code == 404
    assert (
        await outsider_client.get(f"{WS}/{ws}/files/{file_id}/download")
    ).status_code == 404


# --- Listing and revoking grants ------------------------------------------------


async def test_an_owner_lists_grants_with_addresses(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-l1@example.com")
    _, member = await new_user(make_client, "m7-l1-m@example.com")
    await _member(db, ws, member, WorkspaceRole.VIEWER)
    file_id = (await upload_file(owner, ws, "a.txt")).json()["id"]
    await owner.post(
        f"{WS}/{ws}/files/{file_id}/grants",
        json={"user_id": member["user"]["id"], "role": "editor"},
    )

    listed = (await owner.get(f"{WS}/{ws}/files/{file_id}/grants")).json()
    assert [(g["email"], g["role"]) for g in listed] == [("m7-l1-m@example.com", "editor")]


async def test_an_editor_cannot_read_who_has_been_granted_what(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-l2@example.com")
    editor, editor_session = await new_user(make_client, "m7-l2-e@example.com")
    await _member(db, ws, editor_session, WorkspaceRole.EDITOR)
    file_id = (await upload_file(owner, ws, "a.txt")).json()["id"]
    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "F"})).json()["id"]

    assert (await editor.get(f"{WS}/{ws}/files/{file_id}/grants")).status_code == 403
    assert (await editor.get(f"{WS}/{ws}/folders/{folder}/grants")).status_code == 403


async def test_a_folder_grant_can_finally_be_revoked(make_client, db):
    """Before M7 a folder grant, once made, was permanent — and narrowing a grant is how
    access is taken away."""
    owner, ws, _ = await _workspace(make_client, "m7-r1@example.com")
    member_client, member = await new_user(make_client, "m7-r1-m@example.com")
    await _member(db, ws, member, WorkspaceRole.VIEWER)
    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Team"})).json()["id"]

    await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": member["user"]["id"], "role": "editor"},
    )
    # The grant works: the member can create inside the folder.
    made = await member_client.post(
        f"{WS}/{ws}/folders", json={"name": "Inside", "parent_folder_id": folder}
    )
    assert made.status_code == 201

    revoked = await owner.delete(f"{WS}/{ws}/folders/{folder}/grants/{member['user']['id']}")
    assert revoked.status_code == 204
    assert (await owner.get(f"{WS}/{ws}/folders/{folder}/grants")).json() == []

    # Back to their workspace role, viewer: creating inside is refused again.
    again = await member_client.post(
        f"{WS}/{ws}/folders", json={"name": "Again", "parent_folder_id": folder}
    )
    assert again.status_code == 403

    entry = (await _audits(db, ws, AuditAction.PERMISSION_CHANGED))[-1]
    assert entry.audit_metadata["revoked"] is True
    assert entry.audit_metadata["label"] == "Team"


async def test_an_editor_cannot_revoke_a_folder_grant(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-r2@example.com")
    editor, editor_session = await new_user(make_client, "m7-r2-e@example.com")
    await _member(db, ws, editor_session, WorkspaceRole.EDITOR)
    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "F"})).json()["id"]
    await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": editor_session["user"]["id"], "role": "viewer"},
    )
    refused = await editor.delete(
        f"{WS}/{ws}/folders/{folder}/grants/{editor_session['user']['id']}"
    )
    assert refused.status_code == 403


# --- Downloading an earlier version ---------------------------------------------


async def test_an_earlier_version_downloads_under_its_own_number(
    make_client, db, stub_storage
):
    owner, ws, _ = await _workspace(make_client, "m7-d1@example.com")
    first = (await upload_file(owner, ws, "report.pdf")).json()
    await upload_file(owner, ws, "report.pdf")
    versions = (await owner.get(f"{WS}/{ws}/files/{first['id']}/versions")).json()
    oldest = next(v for v in versions if v["version_number"] == 1)

    got = await owner.get(
        f"{WS}/{ws}/files/{first['id']}/download", params={"version_id": oldest["id"]}
    )
    assert got.status_code == 200
    assert stub_storage["download_urls"][-1]["download_filename"] == "report (version 1).pdf"

    # The audit row says which version, and carries the label M6 failed to add.
    entry = (await _audits(db, ws, AuditAction.DOWNLOAD))[-1]
    assert entry.audit_metadata["label"] == "report.pdf"
    assert entry.audit_metadata["version"] == 1


async def test_a_version_of_another_file_is_indistinguishable_from_none(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-d2@example.com")
    a = (await upload_file(owner, ws, "a.txt")).json()["id"]
    b = (await upload_file(owner, ws, "b.txt")).json()["id"]
    b_version = (await owner.get(f"{WS}/{ws}/files/{b}/versions")).json()[0]["id"]

    wrong = await owner.get(f"{WS}/{ws}/files/{a}/download", params={"version_id": b_version})
    assert wrong.status_code == 404


async def test_an_old_version_is_judged_by_its_own_scan_result(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-d3@example.com")
    file_id = (await upload_file(owner, ws, "x.bin")).json()["id"]
    await upload_file(owner, ws, "x.bin")
    old = (
        await db.execute(
            select(FileVersion).where(
                FileVersion.file_id == uuid.UUID(file_id), FileVersion.version_number == 1
            )
        )
    ).scalar_one()
    old.scan_status = ScanStatus.FLAGGED
    await db.commit()

    refused = await owner.get(
        f"{WS}/{ws}/files/{file_id}/download", params={"version_id": str(old.id)}
    )
    assert refused.status_code == 403
    # The current version, clean, is unaffected.
    assert (await owner.get(f"{WS}/{ws}/files/{file_id}/download")).status_code == 200


# --- Rollback (Architecture §4.9) ------------------------------------------------


async def _two_versions(owner, ws):
    first = (await upload_file(owner, ws, "plan.docx", size_bytes=1000)).json()
    await upload_file(owner, ws, "plan.docx", size_bytes=2000)
    versions = (await owner.get(f"{WS}/{ws}/files/{first['id']}/versions")).json()
    return first["id"], {v["version_number"]: v for v in versions}


async def test_rollback_copies_forward_as_a_new_current_version(
    make_client, db, stub_storage, request
):
    owner, ws, _ = await _workspace(make_client, "m7-rb1@example.com")
    file_id, versions = await _two_versions(owner, ws)
    used_before = (await owner.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"]

    restored = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{versions[1]['id']}/restore"
    )
    assert restored.status_code == 201, restored.text
    body = restored.json()
    assert body["version_number"] == 3
    assert body["is_current"] is True
    assert body["restored_from_version_number"] == 1
    assert body["size_bytes"] == 1000

    # Server-side copy from version 1's key to the new version's key.
    source_key = (await db.get(FileVersion, uuid.UUID(versions[1]["id"]))).storage_key
    new = await db.get(FileVersion, uuid.UUID(body["id"]))
    assert stub_storage["copies"] == [(source_key, new.storage_key)]
    assert new.storage_key != source_key

    # History is intact and linear: 1, 2, 3 — nothing rewritten.
    after = (await owner.get(f"{WS}/{ws}/files/{file_id}/versions")).json()
    assert sorted(v["version_number"] for v in after) == [1, 2, 3]
    file = await db.get(File, uuid.UUID(file_id))
    await db.refresh(file)
    assert file.current_version_id == new.id
    assert file.size_bytes == 1000

    # It counts toward quota, exactly like re-uploading the same bytes.
    used_after = (await owner.get(f"{WS}/{ws}/quota")).json()["storage_used_bytes"]
    assert used_after == used_before + 1000

    # The checksum chain runs, so the scanner answers from its cache.
    dispatched = [c for c in request.node.dispatched_tasks if c[0] == "compute_checksum"]
    assert dispatched[-1][1] == (body["id"],)

    entry = (await _audits(db, ws, AuditAction.VERSION_ROLLED_BACK))[-1]
    assert entry.audit_metadata == {
        "label": "plan.docx",
        "restored_version": 1,
        "new_version": 3,
        "file_version_id": body["id"],
    }


async def test_rollback_carries_the_checksum_across(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-rb2@example.com")
    file_id, versions = await _two_versions(owner, ws)
    source = await db.get(FileVersion, uuid.UUID(versions[1]["id"]))
    source.checksum_sha256 = "a" * 64
    await db.commit()

    body = (
        await owner.post(f"{WS}/{ws}/files/{file_id}/versions/{versions[1]['id']}/restore")
    ).json()
    new = await db.get(FileVersion, uuid.UUID(body["id"]))
    assert new.checksum_sha256 == "a" * 64


async def test_restoring_the_current_version_is_refused(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-rb3@example.com")
    file_id, versions = await _two_versions(owner, ws)
    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{versions[2]['id']}/restore"
    )
    assert refused.status_code == 409


async def test_a_flagged_version_cannot_be_laundered_by_rollback(
    make_client, db, stub_storage
):
    owner, ws, _ = await _workspace(make_client, "m7-rb4@example.com")
    file_id, versions = await _two_versions(owner, ws)
    flagged = await db.get(FileVersion, uuid.UUID(versions[1]["id"]))
    flagged.scan_status = ScanStatus.FLAGGED
    await db.commit()

    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{versions[1]['id']}/restore"
    )
    assert refused.status_code == 409
    assert "flagged" in refused.json()["detail"]
    assert stub_storage["copies"] == []


async def test_a_viewer_cannot_roll_back(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-rb5@example.com")
    viewer, viewer_session = await new_user(make_client, "m7-rb5-v@example.com")
    await _member(db, ws, viewer_session, WorkspaceRole.VIEWER)
    file_id, versions = await _two_versions(owner, ws)
    refused = await viewer.post(
        f"{WS}/{ws}/files/{file_id}/versions/{versions[1]['id']}/restore"
    )
    assert refused.status_code == 403


async def test_rollback_over_quota_is_refused_before_any_copy(
    make_client, db, stub_storage
):
    owner, ws, _ = await _workspace(make_client, "m7-rb6@example.com")
    file_id, versions = await _two_versions(owner, ws)
    workspace = await db.get(Workspace, uuid.UUID(ws))
    await db.refresh(workspace)
    workspace.storage_quota_bytes = workspace.storage_used_bytes + 10
    await db.commit()

    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{versions[1]['id']}/restore"
    )
    assert refused.status_code == 413
    assert stub_storage["copies"] == []


async def test_a_version_from_another_file_cannot_be_restored_here(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-rb7@example.com")
    file_id, _ = await _two_versions(owner, ws)
    other = (await upload_file(owner, ws, "other.txt")).json()["id"]
    other_version = (await owner.get(f"{WS}/{ws}/files/{other}/versions")).json()[0]["id"]
    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{other_version}/restore"
    )
    assert refused.status_code == 404


async def test_versions_say_who_uploaded_them(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-rb8@example.com")
    file_id, _ = await _two_versions(owner, ws)
    versions = (await owner.get(f"{WS}/{ws}/files/{file_id}/versions")).json()
    assert {v["uploaded_by_email"] for v in versions} == {"m7-rb8@example.com"}


# --- The trash (Architecture §6b) -------------------------------------------------


async def test_a_deleted_file_appears_once_with_who_and_when(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-t1@example.com")
    file_id = (await upload_file(owner, ws, "gone.txt", size_bytes=500)).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{file_id}")

    trash = (await owner.get(f"{WS}/{ws}/trash")).json()
    assert trash["retention_days"] == 30
    [entry] = trash["entries"]
    assert entry["kind"] == "file"
    assert entry["name"] == "gone.txt"
    assert entry["files"] == 1 and entry["folders"] == 0
    assert entry["size_bytes"] == 500
    assert entry["deleted_by_email"] == "m7-t1@example.com"
    assert entry["restorable"] is True


async def test_a_deleted_folder_is_one_entry_not_one_per_row(make_client):
    """Restore works per batch, so the list does too — a row per file would offer
    choices the restore cannot honour."""
    owner, ws, _ = await _workspace(make_client, "m7-t2@example.com")
    top = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Projects"})).json()["id"]
    sub = (
        await owner.post(f"{WS}/{ws}/folders", json={"name": "Q3", "parent_folder_id": top})
    ).json()["id"]
    await upload_file(owner, ws, "a.txt", folder_id=top)
    await upload_file(owner, ws, "b.txt", folder_id=sub)
    await owner.delete(f"{WS}/{ws}/folders/{top}")

    [entry] = (await owner.get(f"{WS}/{ws}/trash")).json()["entries"]
    assert entry["kind"] == "folder"
    assert entry["name"] == "Projects"  # the one the user chose, not a descendant
    assert entry["folders"] == 2 and entry["files"] == 2


async def test_an_entry_inside_a_deleted_folder_says_why_it_is_blocked(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-t3@example.com")
    top = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Outer"})).json()["id"]
    file_id = (await upload_file(owner, ws, "inner.txt", folder_id=top)).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{file_id}")  # deleted separately, first
    await owner.delete(f"{WS}/{ws}/folders/{top}")

    entries = {e["name"]: e for e in (await owner.get(f"{WS}/{ws}/trash")).json()["entries"]}
    assert entries["Outer"]["restorable"] is True
    assert entries["inner.txt"]["restorable"] is False
    assert "Restore that first" in entries["inner.txt"]["blocked_reason"]


async def test_restoring_removes_the_entry(make_client):
    owner, ws, _ = await _workspace(make_client, "m7-t4@example.com")
    file_id = (await upload_file(owner, ws, "back.txt")).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{file_id}")
    batch = (await owner.get(f"{WS}/{ws}/trash")).json()["entries"][0]["deletion_batch_id"]

    assert (
        await owner.post(f"{WS}/{ws}/files/restore", json={"deletion_batch_id": batch})
    ).status_code == 200
    assert (await owner.get(f"{WS}/{ws}/trash")).json()["entries"] == []


async def test_a_viewer_is_refused_the_trash_with_a_reason(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-t5@example.com")
    viewer, viewer_session = await new_user(make_client, "m7-t5-v@example.com")
    await _member(db, ws, viewer_session, WorkspaceRole.VIEWER)
    refused = await viewer.get(f"{WS}/{ws}/trash")
    assert refused.status_code == 403
    assert "restore" in refused.json()["detail"]


async def test_one_workspaces_trash_never_shows_anothers(make_client):
    owner, first, _ = await _workspace(make_client, "m7-t6@example.com")
    second = (await owner.post(WS, json={"name": "Second"})).json()["id"]
    a = (await upload_file(owner, first, "in-first.txt")).json()["id"]
    b = (await upload_file(owner, second, "in-second.txt")).json()["id"]
    await owner.delete(f"{WS}/{first}/files/{a}")
    await owner.delete(f"{WS}/{second}/files/{b}")

    names = {e["name"] for e in (await owner.get(f"{WS}/{first}/trash")).json()["entries"]}
    assert names == {"in-first.txt"}


# --- The retention sweep ----------------------------------------------------------


@pytest.fixture
def sweep_session(db, monkeypatch):
    """Lend the maintenance task the test session (same reasoning as the notification
    tests: a second connection cannot see rows inside a rolled-back transaction)."""

    class _Lent:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(maintenance_tasks, "SessionLocal", _Lent)
    return db


async def _age_batch(db, file_id: str, days: int) -> list[FileVersion]:
    versions = list(
        (
            await db.execute(
                select(FileVersion).where(FileVersion.file_id == uuid.UUID(file_id))
            )
        ).scalars()
    )
    file = await db.get(File, uuid.UUID(file_id))
    stamp = datetime.now(UTC) - timedelta(days=days)
    file.deleted_at = stamp
    for v in versions:
        v.deleted_at = stamp
    await db.commit()
    return versions


async def test_the_sweep_purges_old_deletions_and_stamps_them(
    make_client, db, stub_storage, sweep_session
):
    owner, ws, _ = await _workspace(make_client, "m7-p1@example.com")
    old = (await upload_file(owner, ws, "old.txt")).json()["id"]
    recent = (await upload_file(owner, ws, "recent.txt")).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{old}")
    await owner.delete(f"{WS}/{ws}/files/{recent}")
    aged = await _age_batch(db, old, days=31)

    result = await maintenance_tasks._purge_expired()
    assert result["purged"] >= 1
    assert aged[0].storage_key in stub_storage["purged"]
    await db.refresh(aged[0])
    assert aged[0].purged_at is not None

    # A deletion inside the window is untouched.
    recent_version = (
        await db.execute(select(FileVersion).where(FileVersion.file_id == uuid.UUID(recent)))
    ).scalar_one()
    assert recent_version.storage_key not in stub_storage["purged"]
    assert recent_version.purged_at is None


async def test_a_purge_storage_refuses_is_not_recorded_as_done(
    make_client, db, stub_storage, sweep_session
):
    """The reason purge_object raises where delete_object logs: stamping purged_at on a
    failed delete would record bytes as gone that are still in the bucket."""
    owner, ws, _ = await _workspace(make_client, "m7-p2@example.com")
    file_id = (await upload_file(owner, ws, "stubborn.txt")).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{file_id}")
    [version] = await _age_batch(db, file_id, days=40)
    stub_storage["purge_failures"].add(version.storage_key)

    result = await maintenance_tasks._purge_expired()
    assert result["failed"] >= 1
    await db.refresh(version)
    assert version.purged_at is None

    # Next run, storage cooperates, and the row is picked up again.
    stub_storage["purge_failures"].clear()
    await maintenance_tasks._purge_expired()
    await db.refresh(version)
    assert version.purged_at is not None


async def test_a_second_sweep_does_not_purge_twice(
    make_client, db, stub_storage, sweep_session
):
    owner, ws, _ = await _workspace(make_client, "m7-p3@example.com")
    file_id = (await upload_file(owner, ws, "once.txt")).json()["id"]
    await owner.delete(f"{WS}/{ws}/files/{file_id}")
    [version] = await _age_batch(db, file_id, days=35)

    await maintenance_tasks._purge_expired()
    await maintenance_tasks._purge_expired()
    assert stub_storage["purged"].count(version.storage_key) == 1


async def test_restore_refuses_a_purged_batch(make_client, db, stub_storage, sweep_session):
    owner, ws, _ = await _workspace(make_client, "m7-p4@example.com")
    file_id = (await upload_file(owner, ws, "late.txt")).json()["id"]
    receipt = (await owner.delete(f"{WS}/{ws}/files/{file_id}")).json()
    await _age_batch(db, file_id, days=31)
    await maintenance_tasks._purge_expired()

    refused = await owner.post(
        f"{WS}/{ws}/files/restore", json={"deletion_batch_id": receipt["deletion_batch_id"]}
    )
    assert refused.status_code == 410
    assert "permanently removed" in refused.json()["detail"]
    # And it no longer offers itself in the trash.
    assert (await owner.get(f"{WS}/{ws}/trash")).json()["entries"] == []


async def test_restore_refuses_past_retention_even_before_the_sweep_ran(make_client, db):
    """Otherwise a restore at day 30 could race the nightly purge and revive rows whose
    bytes vanish minutes later."""
    owner, ws, _ = await _workspace(make_client, "m7-p5@example.com")
    top = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Old"})).json()["id"]
    file_id = (await upload_file(owner, ws, "in-old.txt", folder_id=top)).json()["id"]
    receipt = (await owner.delete(f"{WS}/{ws}/folders/{top}")).json()
    await _age_batch(db, file_id, days=31)

    refused = await owner.post(
        f"{WS}/{ws}/folders/restore", json={"deletion_batch_id": receipt["deletion_batch_id"]}
    )
    assert refused.status_code == 410


# --- The permission indicator (Design §5) -----------------------------------------


async def test_the_listing_carries_each_items_role_and_share_state(make_client, db):
    owner, ws, owner_session = await _workspace(make_client, "m7-i1@example.com")
    editor, editor_session = await new_user(make_client, "m7-i1-e@example.com")
    await _member(db, ws, editor_session, WorkspaceRole.EDITOR)

    locked = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Locked"})).json()["id"]
    await owner.post(f"{WS}/{ws}/folders", json={"name": "Open"})
    shared = (await upload_file(owner, ws, "shared.txt")).json()["id"]
    await upload_file(owner, ws, "plain.txt")

    # Narrow the editor to viewer on one folder, and share one file twice — one link
    # live, one revoked. Only the live one should count.
    await owner.post(
        f"{WS}/{ws}/folders/{locked}/grants",
        json={"user_id": editor_session["user"]["id"], "role": "viewer"},
    )
    shares_url = f"{WS}/{ws}/files/{shared}/shares"
    live = (await owner.post(shares_url, json={"link_type": "public"})).json()
    dead = (await owner.post(shares_url, json={"link_type": "public"})).json()
    await owner.delete(f"{WS}/{ws}/shares/{dead['id']}")
    assert live["id"] != dead["id"]

    listing = (await editor.get(f"{WS}/{ws}/folders")).json()
    folders = {f["name"]: f for f in listing["folders"]}
    files = {f["name"]: f for f in listing["files"]}

    assert folders["Locked"]["my_role"] == "viewer"  # narrowed
    assert folders["Open"]["my_role"] == "editor"  # workspace role
    assert folders["Locked"]["explicit_grants"] == 1
    assert files["shared.txt"]["active_share_links"] == 1
    assert files["plain.txt"]["active_share_links"] == 0


async def test_an_inherited_grant_reaches_the_files_inside(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-i2@example.com")
    editor, editor_session = await new_user(make_client, "m7-i2-e@example.com")
    await _member(db, ws, editor_session, WorkspaceRole.EDITOR)
    folder = (await owner.post(f"{WS}/{ws}/folders", json={"name": "Narrow"})).json()["id"]
    await upload_file(owner, ws, "inside.txt", folder_id=folder)
    await owner.post(
        f"{WS}/{ws}/folders/{folder}/grants",
        json={"user_id": editor_session["user"]["id"], "role": "viewer"},
    )
    listing = (await editor.get(f"{WS}/{ws}/folders/{folder}")).json()
    assert listing["files"][0]["my_role"] == "viewer"


async def test_an_expired_link_does_not_count_as_shared(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m7-i3@example.com")
    file_id = (await upload_file(owner, ws, "e.txt")).json()["id"]
    created = (
        await owner.post(f"{WS}/{ws}/files/{file_id}/shares", json={"link_type": "public"})
    ).json()
    link = await db.get(ShareLink, uuid.UUID(created["id"]))
    link.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()

    listing = (await owner.get(f"{WS}/{ws}/folders")).json()
    assert listing["files"][0]["active_share_links"] == 0


# --- Every workspace audit write carries a label (a structural guard) ------------


ACCOUNT_LEVEL = {"AuditAction.LOGIN", "AuditAction.LOGIN_FAILED", "AuditAction.LOGOUT"}


def test_every_workspace_audit_write_records_a_label():
    """M6 claimed every audit site recorded a label. It found them by searching for
    `metadata={` blocks, so a call with no metadata at all — the download — was never
    seen, and nor were two grant writes whose text differed from the pattern.

    This reads the code's syntax tree instead: any `audit.record(...)` for a
    workspace-scoped action must pass a metadata dict literal with a "label" key.
    Account-level actions belong to no workspace log (Architecture §4.7) and are exempt.
    """
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    missing = []
    for path in sorted(app_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "record"
                and getattr(getattr(node.func, "value", None), "id", None) == "audit"
            ):
                continue
            keywords = {k.arg: k.value for k in node.keywords}
            action = ast.unparse(keywords["action"]) if "action" in keywords else "?"
            if action in ACCOUNT_LEVEL:
                continue
            metadata = keywords.get("metadata")
            has_label = isinstance(metadata, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "label" for k in metadata.keys
            )
            # A conditional action (share accessed/denied) is still workspace-scoped.
            if not has_label:
                missing.append(f"{path.name}:{node.lineno} {action}")
    assert missing == [], "audit writes without a label: " + ", ".join(missing)
