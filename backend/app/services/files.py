"""File and version operations (Schema.md §6-7, Architecture.md §2, §6, §6a)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import File, FileVersion, Folder
from app.models.enums import ScanStatus
from app.models.identity import User, Workspace
from app.services import folders as folder_service
from app.services import quota, storage

# Schema.md §7. Without a cap, one repeatedly re-uploaded file can exhaust a
# workspace's quota with no way to reclaim the space short of deleting the file.
MAX_RETAINED_VERSIONS = 10


class FileError(Exception):
    """Client-safe file operation failure."""


class NameConflict(FileError):
    pass


class UploadStateError(FileError):
    """The upload session is not in a state where this operation makes sense."""


def initial_scan_status() -> ScanStatus:
    """What a freshly uploaded version starts as.

    Scanning arrives at M4. Until then an upload is marked `scan_skipped`, not `clean`:
    the UI badge says "not scanned", which is the truth. Marking it clean would make
    the platform's core trust signal lie for two milestones (Design.md §5).
    """
    return ScanStatus.PENDING if settings.scanning_enabled else ScanStatus.SCAN_SKIPPED


async def get_file(
    db: AsyncSession, workspace_id: uuid.UUID, file_id: uuid.UUID
) -> File | None:
    result = await db.execute(
        select(File).where(
            File.id == file_id,
            File.workspace_id == workspace_id,
            File.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def _next_version_number(db: AsyncSession, file_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(FileVersion.version_number), 0)).where(
            FileVersion.file_id == file_id
        )
    )
    return int(result.scalar_one()) + 1


async def start_upload(
    db: AsyncSession,
    *,
    workspace: Workspace,
    folder_id: uuid.UUID | None,
    name: str,
    mime_type: str,
    size_bytes: int,
    user: User,
) -> tuple[File, FileVersion, list[dict]]:
    """Architecture.md §2 steps 1-3: validate, reserve quota, open the session.

    Re-uploading an existing name creates a new VERSION of that file rather than a
    second file — which is what the unique index on (workspace, folder, name) makes
    well-defined.
    """
    if folder_id is not None:
        folder = await folder_service.get_folder(db, workspace.id, folder_id)
        if folder is None:
            raise FileError("Folder not found.")

    # Quota and the provider ceiling are both checked before any storage call, under a
    # row lock so two concurrent uploads cannot both pass (Architecture.md §6).
    await quota.reserve(db, workspace.id, size_bytes)

    result = await db.execute(
        select(File).where(
            File.workspace_id == workspace.id,
            File.folder_id.is_(folder_id) if folder_id is None else File.folder_id == folder_id,
            File.name == name,
            File.deleted_at.is_(None),
        )
    )
    file = result.scalar_one_or_none()

    if file is None:
        file = File(
            workspace_id=workspace.id,
            folder_id=folder_id,
            name=name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            scan_status=initial_scan_status(),
            created_by=user.id,
        )
        db.add(file)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise NameConflict("A file with that name already exists here.") from exc

    version = FileVersion(
        file_id=file.id,
        version_number=await _next_version_number(db, file.id),
        storage_key="",  # set below, once the version id exists
        size_bytes=size_bytes,
        uploaded_by=user.id,
        scan_status=initial_scan_status(),
        upload_started_at=datetime.now(UTC),
    )
    db.add(version)
    await db.flush()

    version.storage_key = storage.build_storage_key(
        workspace.id, file.id, version.id, name
    )
    version.upload_id = storage.create_multipart_upload(version.storage_key, mime_type)
    await db.flush()

    parts = [
        {
            "part_number": number,
            "size_bytes": part_size,
            "url": storage.presign_part(version.storage_key, version.upload_id, number),
        }
        for number, part_size in storage.plan_parts(size_bytes)
    ]
    return file, version, parts


async def complete_upload(
    db: AsyncSession,
    *,
    workspace: Workspace,
    file: File,
    version: FileVersion,
    parts: list[dict],
) -> tuple[File, list[FileVersion]]:
    """Architecture.md §2 steps 5-6, plus version pruning.

    Returns (file, pruned_versions) — the caller purges the pruned objects.
    """
    if version.upload_id is None:
        raise UploadStateError("This upload has already been completed or aborted.")

    storage.complete_multipart_upload(
        version.storage_key,
        version.upload_id,
        [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts],
    )

    version.upload_id = None
    version.upload_started_at = None
    version.scan_status = initial_scan_status()

    file.current_version_id = version.id
    file.size_bytes = version.size_bytes
    file.scan_status = version.scan_status
    file.updated_at = datetime.now(UTC)
    await db.flush()

    pruned = await prune_old_versions(db, workspace=workspace, file=file)
    return file, pruned


async def prune_old_versions(
    db: AsyncSession, *, workspace: Workspace, file: File
) -> list[FileVersion]:
    """Keep at most MAX_RETAINED_VERSIONS, oldest pruned first (Schema.md §7)."""
    result = await db.execute(
        select(FileVersion)
        .where(
            FileVersion.file_id == file.id,
            FileVersion.deleted_at.is_(None),
            FileVersion.upload_id.is_(None),
        )
        .order_by(FileVersion.version_number.desc())
    )
    live = list(result.scalars().all())
    if len(live) <= MAX_RETAINED_VERSIONS:
        return []

    doomed = [v for v in live[MAX_RETAINED_VERSIONS:] if v.id != file.current_version_id]
    now = datetime.now(UTC)
    released = 0
    for version in doomed:
        version.deleted_at = now
        # No batch id: a pruned version is not restorable, it is superseded.
        released += version.size_bytes
    await db.flush()
    if released:
        await quota.release(db, workspace.id, released)
    return doomed


async def abort_upload(
    db: AsyncSession, *, workspace: Workspace, version: FileVersion
) -> None:
    """Abandon an in-flight upload and give the reserved space back."""
    if version.upload_id:
        storage.abort_multipart_upload(version.storage_key, version.upload_id)
    version.upload_id = None
    version.deleted_at = datetime.now(UTC)
    await db.flush()
    await quota.release(db, workspace.id, version.size_bytes)


async def rename_or_move(
    db: AsyncSession,
    *,
    file: File,
    name: str | None = None,
    new_folder_id: uuid.UUID | None = None,
    move: bool = False,
) -> File:
    if move:
        if new_folder_id is not None:
            folder = await folder_service.get_folder(db, file.workspace_id, new_folder_id)
            if folder is None:
                raise FileError("Target folder not found.")
        file.folder_id = new_folder_id
    if name is not None:
        file.name = name
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise NameConflict("A file with that name already exists there.") from exc
    return file


async def soft_delete(db: AsyncSession, *, workspace: Workspace, file: File) -> uuid.UUID:
    """Soft-delete a file and its versions under one batch id."""
    batch_id = uuid.uuid4()
    now = datetime.now(UTC)

    result = await db.execute(
        select(FileVersion).where(
            FileVersion.file_id == file.id, FileVersion.deleted_at.is_(None)
        )
    )
    released = 0
    for version in result.scalars().all():
        version.deleted_at = now
        version.deletion_batch_id = batch_id
        if version.upload_id is None:
            released += version.size_bytes

    file.deleted_at = now
    file.deletion_batch_id = batch_id
    await db.flush()
    if released:
        await quota.release(db, workspace.id, released)
    return batch_id


async def restore(
    db: AsyncSession, *, workspace: Workspace, batch_id: uuid.UUID
) -> int:
    """Restore one deletion batch of files (Architecture.md §6a)."""
    result = await db.execute(
        select(File).where(
            File.workspace_id == workspace.id, File.deletion_batch_id == batch_id
        )
    )
    files = list(result.scalars().all())
    if not files:
        raise FileError("Nothing to restore for that deletion.")

    versions_result = await db.execute(
        select(FileVersion).where(
            FileVersion.file_id.in_([f.id for f in files]),
            FileVersion.deletion_batch_id == batch_id,
        )
    )
    versions = list(versions_result.scalars().all())

    reclaim = sum(v.size_bytes for v in versions if v.upload_id is None)
    if reclaim:
        await quota.reserve(db, workspace.id, reclaim)

    for file in files:
        if file.folder_id is not None:
            folder = await db.get(Folder, file.folder_id)
            if folder is None or folder.deleted_at is not None:
                raise FileError(
                    "The folder this file was in has also been deleted. Restore that first."
                )

    for row in (*files, *versions):
        row.deleted_at = None
        row.deletion_batch_id = None
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise NameConflict(
            "A file with that name now exists where this was deleted from."
        ) from exc
    return len(files)


async def download_url(db: AsyncSession, file: File) -> str:
    """A short-lived signed URL, but only if the scan status permits it.

    Architecture.md §3 is the single source of truth for whether bytes may be served.
    `pending` and `flagged` are never downloadable, including to the owner.
    """
    if not file.scan_status.is_downloadable:
        raise FileError(
            f"This file cannot be downloaded while its scan status is "
            f"'{file.scan_status.value}'."
        )
    if file.current_version_id is None:
        raise FileError("This file has no completed upload yet.")
    version = await db.get(FileVersion, file.current_version_id)
    if version is None or version.deleted_at is not None:
        raise FileError("This file has no completed upload yet.")
    return storage.generate_download_url(version.storage_key, download_filename=file.name)


async def list_in_flight(
    db: AsyncSession, workspace_id: uuid.UUID
) -> list[tuple[FileVersion, File, int, int]]:
    """Uploads started but never completed, with real progress from the provider.

    Architecture.md §2a. Progress is read from storage rather than remembered locally,
    because the client's record of it died with the page that started the upload.
    """
    result = await db.execute(
        select(FileVersion, File)
        .join(File, File.id == FileVersion.file_id)
        .where(
            File.workspace_id == workspace_id,
            FileVersion.upload_id.is_not(None),
            FileVersion.deleted_at.is_(None),
        )
        .order_by(FileVersion.upload_started_at.desc())
    )
    rows = []
    for version, file in result.all():
        try:
            parts = storage.list_parts(version.storage_key, version.upload_id)
        except storage.StorageError:
            # A session the provider no longer knows about is not resumable, but it
            # should not take the whole listing down with it.
            parts = []
        # Sizes come from our own plan: the provider reports which parts exist, not
        # how large they are.
        sizes = dict(storage.plan_parts(version.size_bytes))
        uploaded = sum(sizes.get(p["part_number"], 0) for p in parts)
        rows.append((version, file, uploaded, len(parts)))
    return rows


async def resume_state(
    db: AsyncSession, *, workspace: Workspace, version: FileVersion, file: File
) -> dict:
    """Completed parts plus freshly signed URLs for what is left."""
    if version.upload_id is None:
        raise UploadStateError("That upload has already been completed or aborted.")

    landed = storage.list_parts(version.storage_key, version.upload_id)
    done = {p["part_number"]: p["etag"] for p in landed}

    plan = storage.plan_parts(version.size_bytes)

    # Authoritative byte offsets for every part, so the client never has to infer them.
    layout = []
    offset = 0
    for number, size in plan:
        layout.append({"part_number": number, "offset": offset, "size_bytes": size})
        offset += size

    remaining = [
        {
            "part_number": number,
            "size_bytes": size,
            # Always re-sign: presigned URLs are short-lived by design, and reusing a
            # stale one fails in a way that looks like a server fault.
            "url": storage.presign_part(version.storage_key, version.upload_id, number),
        }
        for number, size in plan
        if number not in done
    ]
    return {
        "version_id": version.id,
        "file_id": file.id,
        "file_name": file.name,
        "total_bytes": version.size_bytes,
        "completed_parts": [
            {"part_number": n, "etag": e} for n, e in sorted(done.items())
        ],
        "remaining_parts": remaining,
        "layout": layout,
        "uploaded_bytes": sum(dict(plan).get(n, 0) for n in done),
    }
