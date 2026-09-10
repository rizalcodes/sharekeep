"""M4: the access rule, the verdict cache, and the admin override.

Architecture.md §3 (what may be served), §3c (verdict cache), §3d (override).

The VirusTotal client and the budget limiter are verified against the real API and the
real Redis in `scratchpad` check scripts rather than mocked here — a stub kinder than
the provider is how M3 shipped a `KeyError` behind a green suite. What *is* tested here
is our own logic: who may read a file, what gets cached, and what an override does and
does not change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import File, FileVersion, ScanResult
from app.models.enums import AuditAction, ScanStatus, WorkspaceRole
from app.models.identity import AuditLog, WorkspaceMember
from app.services import scan
from app.services.virustotal import Verdict
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"


async def _workspace(make_client, email: str):
    client, _ = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Scanning"})).json()["id"]
    return client, ws


def _version(status: ScanStatus, *, overridden: bool = False) -> FileVersion:
    """An unsaved version, which is all the access rule needs."""
    version = FileVersion(
        id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        version_number=1,
        storage_key="k",
        size_bytes=1,
        uploaded_by=uuid.uuid4(),
        scan_status=status,
    )
    if overridden:
        version.scan_override_at = datetime.now(UTC)
        version.scan_override_by = uuid.uuid4()
        version.scan_override_reason = "reviewed by hand, known good"
    return version


# --------------------------------------------------------------- the access table ----


@pytest.mark.parametrize(
    ("status", "downloadable"),
    [
        (ScanStatus.PENDING, False),
        (ScanStatus.SCANNING, False),
        (ScanStatus.CLEAN, True),
        (ScanStatus.SCAN_SKIPPED, True),
        (ScanStatus.FLAGGED, False),
        (ScanStatus.SCAN_FAILED, False),
    ],
)
def test_the_access_table_without_an_override(status, downloadable):
    """Architecture.md §3 in one assertion per row."""
    assert scan.version_is_downloadable(_version(status)) is downloadable


@pytest.mark.parametrize(
    ("status", "downloadable"),
    [
        # The two dead-end states are exactly the ones an override releases.
        (ScanStatus.FLAGGED, True),
        (ScanStatus.SCAN_FAILED, True),
        # An override cannot conjure an answer that does not exist yet: a scan still
        # running has produced nothing to overrule.
        (ScanStatus.PENDING, False),
        (ScanStatus.SCANNING, False),
    ],
)
def test_the_access_table_with_an_override(status, downloadable):
    assert scan.version_is_downloadable(_version(status, overridden=True)) is downloadable


def test_pending_and_flagged_refusals_are_told_apart():
    """A user needs to know whether waiting will help."""
    pending = scan.download_refusal_reason(_version(ScanStatus.PENDING))
    flagged = scan.download_refusal_reason(_version(ScanStatus.FLAGGED))
    failed = scan.download_refusal_reason(_version(ScanStatus.SCAN_FAILED))

    assert "queued" in pending.lower()
    assert "flagged" in flagged.lower()
    assert "could not be scanned" in failed.lower()
    assert len({pending, flagged, failed}) == 3


# ------------------------------------------------------------------ verdict cache ----


async def test_cache_miss_returns_none(db):
    assert await scan.cached_verdict(db, "0" * 64) is None


async def test_a_fresh_verdict_is_reused_and_marked_as_cached(db):
    checksum = "a" * 64
    await scan.remember_verdict(
        db, checksum, Verdict(status=ScanStatus.CLEAN, detail={"stats": {"malicious": 0}})
    )
    await db.flush()

    cached = await scan.cached_verdict(db, checksum)
    assert cached is not None
    assert cached.status is ScanStatus.CLEAN
    # The marker matters: a version's scan_detail must not imply a request was spent.
    assert cached.detail["from_cache"] is True
    assert "cached_at" in cached.detail


async def test_a_stale_verdict_is_treated_as_absent(db):
    """A file clean today may be flagged next month (Architecture.md §3c)."""
    checksum = "b" * 64
    db.add(
        ScanResult(
            id=uuid.uuid4(),
            checksum_sha256=checksum,
            verdict=ScanStatus.CLEAN,
            detail={},
            checked_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    await db.flush()

    assert await scan.cached_verdict(db, checksum) is None


async def test_re_lookup_replaces_the_row_rather_than_accumulating(db):
    """This is a cache, not a history: one row per hash."""
    from sqlalchemy import func, select

    checksum = "c" * 64
    await scan.remember_verdict(db, checksum, Verdict(ScanStatus.CLEAN, {"n": 1}))
    await db.flush()
    await scan.remember_verdict(db, checksum, Verdict(ScanStatus.FLAGGED, {"n": 2}))
    await db.flush()

    rows = await db.execute(
        select(func.count()).select_from(ScanResult).where(
            ScanResult.checksum_sha256 == checksum
        )
    )
    assert rows.scalar_one() == 1
    cached = await scan.cached_verdict(db, checksum)
    assert cached.status is ScanStatus.FLAGGED
    assert cached.detail["n"] == 2


@pytest.mark.parametrize(
    "status",
    [ScanStatus.PENDING, ScanStatus.SCANNING, ScanStatus.SCAN_FAILED, ScanStatus.SCAN_SKIPPED],
)
async def test_unsettled_verdicts_are_refused_by_the_cache(db, status):
    """Caching a non-answer would serve it back as though it were an answer."""
    with pytest.raises(ValueError, match="settled"):
        await scan.remember_verdict(db, "d" * 64, Verdict(status=status, detail={}))


async def test_the_database_also_refuses_an_unsettled_verdict(db):
    """The service check is a convention; the CHECK constraint is the guarantee."""
    from sqlalchemy.exc import IntegrityError

    db.add(
        ScanResult(
            id=uuid.uuid4(),
            checksum_sha256="e" * 64,
            verdict=ScanStatus.PENDING,
            detail={},
            checked_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# ----------------------------------------------------------------------- override ----


async def _flagged_file(client, ws, db, name="virus.exe"):
    file_id = (await upload_file(client, ws, name)).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    file.scan_status = ScanStatus.FLAGGED
    await db.flush()
    return file, version


async def test_an_owner_can_release_a_flagged_version(make_client, db):
    client, ws = await _workspace(make_client, "m4-owner@example.com")
    file, version = await _flagged_file(client, ws, db)

    response = await client.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "Reviewed manually, this is a known false positive."},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The status is NOT rewritten - nothing re-scanned the file.
    assert body["scan_status"] == ScanStatus.FLAGGED.value
    assert body["is_downloadable"] is True
    assert body["scan_override_at"] is not None
    assert body["scan_override_reason"].startswith("Reviewed manually")


async def test_the_download_works_only_after_the_override(make_client, db):
    client, ws = await _workspace(make_client, "m4-dl@example.com")
    file, version = await _flagged_file(client, ws, db)

    before = await client.get(f"{WS}/{ws}/files/{file.id}/download")
    assert before.status_code == 403

    await client.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "Signed off by the security reviewer."},
    )
    after = await client.get(f"{WS}/{ws}/files/{file.id}/download")
    assert after.status_code == 200


async def test_the_override_is_audited_with_who_and_why(make_client, db):
    from sqlalchemy import select

    client, ws = await _workspace(make_client, "m4-audit@example.com")
    file, version = await _flagged_file(client, ws, db)
    reason = "Internal build flagged by one engine; verified against the source."

    await client.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": reason},
    )

    entries = (
        await db.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.SCAN_OVERRIDE)
        )
    ).scalars().all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_id is not None, "a person released this, so a person is recorded"
    assert entry.audit_metadata["reason"] == reason
    assert entry.audit_metadata["file_version_id"] == str(version.id)


@pytest.mark.parametrize("role", [WorkspaceRole.EDITOR, WorkspaceRole.VIEWER])
async def test_only_an_owner_may_override(make_client, db, role):
    """Adversarial: an editor with rights on the file still cannot overrule the scanner."""
    owner, ws = await _workspace(make_client, f"m4-own-{role.value}@example.com")
    file, version = await _flagged_file(owner, ws, db)

    other, session = await new_user(make_client, f"m4-{role.value}@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws), user_id=uuid.UUID(session["user"]["id"]), role=role
        )
    )
    await db.flush()

    response = await other.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "I would like to download this file please."},
    )
    assert response.status_code == 403


async def test_a_non_member_gets_a_404_not_a_403(make_client, db):
    """Adversarial: existence of the workspace is not disclosed."""
    owner, ws = await _workspace(make_client, "m4-hidden@example.com")
    file, version = await _flagged_file(owner, ws, db)

    outsider, _ = await new_user(make_client, "m4-outsider@example.com")
    response = await outsider.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "Trying my luck against someone else's workspace."},
    )
    assert response.status_code == 404


async def test_a_version_from_another_workspace_cannot_be_paired_with_a_local_file(
    make_client, db
):
    """Adversarial: substituting a foreign version id must not resolve."""
    victim, victim_ws = await _workspace(make_client, "m4-victim@example.com")
    _, victim_version = await _flagged_file(victim, victim_ws, db, name="theirs.exe")

    attacker, attacker_ws = await _workspace(make_client, "m4-attacker@example.com")
    own_file, _ = await _flagged_file(attacker, attacker_ws, db, name="mine.exe")

    response = await attacker.post(
        f"{WS}/{attacker_ws}/files/{own_file.id}/versions/{victim_version.id}/scan-override",
        json={"reason": "Pairing a foreign version id with a file I do own."},
    )
    assert response.status_code == 404

    await db.refresh(victim_version)
    assert victim_version.scan_override_at is None, "the victim's version was not touched"


@pytest.mark.parametrize(
    "status", [ScanStatus.CLEAN, ScanStatus.SCAN_SKIPPED, ScanStatus.PENDING, ScanStatus.SCANNING]
)
async def test_only_flagged_or_failed_versions_can_be_overridden(make_client, db, status):
    client, ws = await _workspace(make_client, f"m4-state-{status.value}@example.com")
    file_id = (await upload_file(client, ws, "f.txt")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = status
    await db.flush()

    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/versions/{version.id}/scan-override",
        json={"reason": "Attempting to override a state with no verdict to overrule."},
    )
    assert response.status_code == 409


async def test_an_override_demands_a_real_reason(make_client, db):
    client, ws = await _workspace(make_client, "m4-reason@example.com")
    file, version = await _flagged_file(client, ws, db)

    for reason in ("", "ok", "        "):
        response = await client.post(
            f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
            json={"reason": reason},
        )
        assert response.status_code == 422, f"accepted {reason!r} as a justification"


async def test_an_override_does_not_carry_to_a_later_upload(make_client, db):
    """It was a judgement about specific content, not a standing exemption."""
    client, ws = await _workspace(make_client, "m4-carry@example.com")
    file, version = await _flagged_file(client, ws, db, name="tool.exe")
    await client.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "Released after manual review of version 1."},
    )

    # A second upload of the same name creates version 2.
    await upload_file(client, ws, "tool.exe")
    await db.refresh(file)
    newer = await db.get(FileVersion, file.current_version_id)

    assert newer.id != version.id
    assert newer.scan_override_at is None
    assert scan.has_override(newer) is False


# ------------------------------------------------------------------------- mirror ----


async def test_a_verdict_on_a_superseded_version_does_not_change_the_file(make_client, db):
    """`files.scan_status` mirrors the CURRENT version only (Schema.md §6).

    Otherwise a slow scan finishing on version 1 would relabel a file that has already
    moved to version 2 — reporting the wrong version's result as the file's own.
    """
    client, ws = await _workspace(make_client, "m4-mirror@example.com")
    file_id = (await upload_file(client, ws, "doc.txt")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    old_version = await db.get(FileVersion, file.current_version_id)

    await upload_file(client, ws, "doc.txt")
    await db.refresh(file)
    assert file.current_version_id != old_version.id
    current_status = file.scan_status

    await scan.apply_verdict(
        db, old_version, Verdict(status=ScanStatus.FLAGGED, detail={"stats": {}})
    )
    await db.flush()
    await db.refresh(file)

    assert old_version.scan_status is ScanStatus.FLAGGED
    assert file.scan_status is current_status, "the superseded scan leaked onto the file"


# ------------------------------------------------------------------ task dispatch ----


async def test_completing_an_upload_queues_the_checksum_task(make_client, dispatched_tasks):
    """The integrity checksum is not optional and not tied to scanning."""
    client, ws = await _workspace(make_client, "m4-queue@example.com")
    dispatched_tasks.clear()
    await upload_file(client, ws, "queued.txt")

    assert [name for name, _, _ in dispatched_tasks] == ["compute_checksum"]


# -------------------------------------------------------------- listing surface ----


async def test_the_listing_resolves_downloadability_server_side(make_client, db):
    """The browser must not have to re-derive the §3 access table."""
    client, ws = await _workspace(make_client, "m4-listing@example.com")
    await upload_file(client, ws, "ok.txt")
    file, version = await _flagged_file(client, ws, db, name="bad.exe")

    listing = (await client.get(f"{WS}/{ws}/folders")).json()
    by_name = {f["name"]: f for f in listing["files"]}

    assert by_name["ok.txt"]["is_downloadable"] is True
    assert by_name["bad.exe"]["is_downloadable"] is False

    await client.post(
        f"{WS}/{ws}/files/{file.id}/versions/{version.id}/scan-override",
        json={"reason": "Released after review by the workspace owner."},
    )
    after = {f["name"]: f for f in (await client.get(f"{WS}/{ws}/folders")).json()["files"]}

    released = after["bad.exe"]
    assert released["is_downloadable"] is True
    # Still flagged, and now carrying who released it and why.
    assert released["scan_status"] == ScanStatus.FLAGGED.value
    assert released["scan_override_at"] is not None
    assert released["scan_override_reason"].startswith("Released after review")


async def test_a_skipped_scan_carries_its_reason_to_the_listing(make_client, db):
    """"Not scanned" without a why invites the reader to assume the best case."""
    client, ws = await _workspace(make_client, "m4-reasoned@example.com")
    file_id = (await upload_file(client, ws, "unknown.bin")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.SCAN_SKIPPED
    version.scan_detail = {"skipped": True, "reason": "not previously seen by the scanner"}
    await db.flush()

    listing = (await client.get(f"{WS}/{ws}/folders")).json()
    row = next(f for f in listing["files"] if f["name"] == "unknown.bin")

    assert row["scan_status"] == ScanStatus.SCAN_SKIPPED.value
    assert row["is_downloadable"] is True
    assert row["scan_reason"] == "not previously seen by the scanner"
