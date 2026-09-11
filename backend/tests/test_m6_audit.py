"""M6 audit log reading (Architecture.md §4.7).

The access rules are the point of this file. An audit log holds the IP address of every
share-link visitor and the activity pattern of every member, so who cannot read it
matters more than who can — and §4.6 had never said, which is why these are written
adversarially rather than as a happy path with a 403 appended.
"""

from __future__ import annotations

import base64
import uuid

from app.models.enums import AuditAction, WorkspaceRole
from app.models.identity import AuditLog
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Audited"})).json()["id"]
    return client, ws, session


def _audit(ws: str) -> str:
    return f"{WS}/{ws}/audit"


# --- Who may read it -------------------------------------------------------------


async def test_an_owner_can_read_their_workspaces_history(make_client):
    client, ws, _ = await _workspace(make_client, "m6-owner@example.com")
    await upload_file(client, ws, "quarterly.pdf")

    page = await client.get(_audit(ws))
    assert page.status_code == 200
    actions = [entry["action"] for entry in page.json()["entries"]]
    assert AuditAction.UPLOAD.value in actions


async def test_an_editor_is_refused_403_not_shown_a_filtered_view(make_client, db):
    """403, not 200-with-less. A partial audit log would be worse than none: it would
    look complete."""
    owner, ws, _ = await _workspace(make_client, "m6-o2@example.com")
    editor, editor_session = await new_user(make_client, "m6-editor@example.com")

    from app.models.identity import WorkspaceMember

    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(editor_session["user"]["id"]),
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.commit()

    refused = await editor.get(_audit(ws))
    assert refused.status_code == 403


async def test_a_viewer_is_refused_too(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m6-o3@example.com")
    viewer, viewer_session = await new_user(make_client, "m6-viewer@example.com")

    from app.models.identity import WorkspaceMember

    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(viewer_session["user"]["id"]),
            role=WorkspaceRole.VIEWER,
        )
    )
    await db.commit()

    assert (await viewer.get(_audit(ws))).status_code == 403


async def test_a_non_member_gets_404_so_workspace_ids_stay_unenumerable(make_client):
    _, ws, _ = await _workspace(make_client, "m6-o4@example.com")
    stranger, _ = await new_user(make_client, "m6-stranger@example.com")

    refused = await stranger.get(_audit(ws))
    # 404 rather than 403: a 403 would confirm the workspace exists, which is the
    # distinction §4.7 inherits from the workspace dependency.
    assert refused.status_code == 404


async def test_an_anonymous_caller_is_refused(client, make_client):
    _, ws, _ = await _workspace(make_client, "m6-o5@example.com")
    anonymous = await make_client()
    assert (await anonymous.get(_audit(ws))).status_code == 401


# --- What it does and does not contain -------------------------------------------


async def test_account_level_events_never_appear_in_a_workspace_log(make_client, db):
    """`login` carries workspace_id = NULL, so it is excluded by construction.

    Asserted because the exclusion is a privacy decision, not an implementation detail:
    an owner must not learn when a collaborator signed in to unrelated work.
    """
    client, ws, _ = await _workspace(make_client, "m6-login@example.com")

    # The signup in the fixture wrote a `login` row. Prove it exists at all, so this
    # test cannot pass merely because nothing was ever written.
    from sqlalchemy import select

    total_logins = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.LOGIN)
        )
    ).scalars().all()
    assert total_logins, "no login rows exist, so this test would pass vacuously"

    page = await client.get(_audit(ws))
    actions = [entry["action"] for entry in page.json()["entries"]]
    assert AuditAction.LOGIN.value not in actions
    assert AuditAction.LOGOUT.value not in actions
    assert page.json()["excludes_account_events"] is True


async def test_a_new_row_records_the_targets_name_at_the_time(make_client):
    """The label is denormalised, not joined.

    The log is append-only; resolving `target_id` at read time would let a rename
    rewrite what the log says happened.
    """
    client, ws, _ = await _workspace(make_client, "m6-label@example.com")
    await upload_file(client, ws, "original-name.pdf")

    page = await client.get(_audit(ws))
    upload = next(
        e for e in page.json()["entries"] if e["action"] == AuditAction.UPLOAD.value
    )
    assert upload["label"] == "original-name.pdf"


async def test_a_refused_upload_is_audited_as_quota_exceeded(make_client, db):
    """QUOTA_EXCEEDED was in the enum from M1 and written by nothing until M6.

    An audit reader that offers a filter which can never match is worse than one that
    omits it, because the empty result reads as "this never happened".
    """
    client, ws, _ = await _workspace(make_client, "m6-quota@example.com")

    from app.models.identity import Workspace

    workspace = await db.get(Workspace, uuid.UUID(ws))
    workspace.storage_quota_bytes = 2048
    await db.commit()

    refused = await upload_file(client, ws, "too-big.bin", size_bytes=999_999)
    assert refused.status_code == 413

    page = await client.get(_audit(ws))
    entry = next(
        (
            e
            for e in page.json()["entries"]
            if e["action"] == AuditAction.QUOTA_EXCEEDED.value
        ),
        None,
    )
    assert entry is not None, "a refused upload wrote no audit row"
    assert entry["metadata"]["attempted_name"] == "too-big.bin"
    assert entry["metadata"]["attempted_bytes"] == 999_999


async def test_the_refused_upload_still_returns_413_and_reserves_nothing(make_client, db):
    """The audit write must not accidentally commit the rejected reservation."""
    client, ws, _ = await _workspace(make_client, "m6-quota2@example.com")

    from app.models.identity import Workspace

    workspace = await db.get(Workspace, uuid.UUID(ws))
    workspace.storage_quota_bytes = 2048
    await db.commit()
    before = workspace.storage_used_bytes

    assert (await upload_file(client, ws, "nope.bin", size_bytes=999_999)).status_code == 413

    await db.refresh(workspace)
    assert workspace.storage_used_bytes == before


# --- Paging and filtering --------------------------------------------------------


async def test_paging_is_keyset_and_does_not_repeat_or_skip_rows(make_client):
    """Keyset, not offset: the table only grows, so an offset shifts under the reader."""
    client, ws, _ = await _workspace(make_client, "m6-page@example.com")
    for index in range(7):
        await upload_file(client, ws, f"file-{index}.bin")

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        url = f"{_audit(ws)}?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(url)).json()
        seen.extend(entry["id"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "paging did not terminate"
    assert len(seen) == len(set(seen)), "a row was returned on two different pages"

    everything = (await client.get(f"{_audit(ws)}?limit=100")).json()["entries"]
    assert len(seen) == len(everything)


async def test_entries_are_newest_first(make_client):
    client, ws, _ = await _workspace(make_client, "m6-order@example.com")
    await upload_file(client, ws, "first.bin")
    await upload_file(client, ws, "second.bin")

    entries = (await client.get(_audit(ws))).json()["entries"]
    stamps = [entry["created_at"] for entry in entries]
    assert stamps == sorted(stamps, reverse=True)


async def test_a_malformed_cursor_is_refused_rather_than_silently_ignored(make_client):
    """Returning page one for a broken cursor would hide a paging bug instead of
    surfacing it."""
    client, ws, _ = await _workspace(make_client, "m6-cursor@example.com")
    assert (await client.get(f"{_audit(ws)}?cursor=not-a-cursor")).status_code == 422

    # Well-formed base64 that decodes to nonsense is still refused.
    junk = base64.urlsafe_b64encode(b"hello|world").decode()
    assert (await client.get(f"{_audit(ws)}?cursor={junk}")).status_code == 422


async def test_filtering_by_action_narrows_without_leaking_other_workspaces(make_client):
    client, ws, _ = await _workspace(make_client, "m6-filter@example.com")
    await upload_file(client, ws, "a.bin")
    await client.post(f"{WS}/{ws}/folders", json={"name": "Reports"})

    only_folders = await client.get(
        f"{_audit(ws)}?action={AuditAction.FOLDER_CREATED.value}"
    )
    actions = {entry["action"] for entry in only_folders.json()["entries"]}
    assert actions == {AuditAction.FOLDER_CREATED.value}


async def test_one_workspaces_log_never_contains_anothers(make_client):
    client, first, _ = await _workspace(make_client, "m6-iso@example.com")
    second = (await client.post(WS, json={"name": "Other"})).json()["id"]

    await upload_file(client, first, "in-first.bin")
    await upload_file(client, second, "in-second.bin")

    labels = {
        entry["label"] for entry in (await client.get(_audit(first))).json()["entries"]
    }
    assert "in-first.bin" in labels
    assert "in-second.bin" not in labels


async def test_the_search_term_matches_the_recorded_label(make_client):
    client, ws, _ = await _workspace(make_client, "m6-search@example.com")
    await upload_file(client, ws, "payroll-2026.xlsx")
    await upload_file(client, ws, "unrelated.txt")

    found = (await client.get(f"{_audit(ws)}?q=payroll")).json()["entries"]
    assert found
    assert all("payroll" in (entry["label"] or "").lower() for entry in found)


async def test_limit_is_capped_so_one_request_cannot_pull_the_whole_table(make_client):
    client, ws, _ = await _workspace(make_client, "m6-limit@example.com")
    assert (await client.get(f"{_audit(ws)}?limit=5000")).status_code == 422


async def test_there_is_no_write_or_delete_path(make_client):
    """`audit_logs` is append-only; the router must not offer a way around that."""
    client, ws, _ = await _workspace(make_client, "m6-append@example.com")
    await upload_file(client, ws, "a.bin")
    entry_id = (await client.get(_audit(ws))).json()["entries"][0]["id"]

    for method, url in (
        ("delete", f"{_audit(ws)}/{entry_id}"),
        ("post", _audit(ws)),
        ("patch", f"{_audit(ws)}/{entry_id}"),
    ):
        response = await getattr(client, method)(url)
        assert response.status_code in (404, 405), f"{method} {url} -> {response.status_code}"
