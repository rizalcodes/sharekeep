"""The trash (Architecture.md §6b).

Editors and owners only — the roles that can delete, and so the ones who can restore.
A viewer shown a list of deletions they cannot act on would be looking at noise.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import DbSession, ViewerAccess
from app.models.enums import WorkspaceRole
from app.services import trash

router = APIRouter(prefix=f"{settings.api_v1_prefix}/workspaces", tags=["trash"])


class TrashEntryOut(BaseModel):
    deletion_batch_id: uuid.UUID
    # Also names the restore endpoint that applies: /folders/restore or /files/restore.
    kind: str
    name: str
    folders: int
    files: int
    size_bytes: int
    deleted_at: datetime
    deleted_by_email: str | None
    purge_after: datetime
    restorable: bool
    blocked_reason: str | None


class TrashOut(BaseModel):
    entries: list[TrashEntryOut]
    retention_days: int


@router.get("/{workspace_id}/trash", response_model=TrashOut)
async def list_trash(access: ViewerAccess, db: DbSession) -> TrashOut:
    workspace, role = access
    if not role.can_act_as(WorkspaceRole.EDITOR):
        # 403 with the reason, not an empty list: an empty trash and a trash you may
        # not see are different answers, and only one of them is true.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only editors and owners can see the trash, because only they can restore.",
        )
    entries = await trash.list_entries(db, workspace.id)
    return TrashOut(
        entries=[TrashEntryOut(**entry.__dict__) for entry in entries],
        retention_days=settings.soft_delete_retention_days,
    )
