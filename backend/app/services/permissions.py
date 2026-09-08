"""Workspace membership and role resolution.

Architecture.md §4 requires a single resolution path: one place to audit, one place to
test adversarially. Every endpoint that touches a workspace goes through here — no
endpoint may query `workspace_members` directly and reach its own conclusion.

M1 covers workspace-level roles (§4.6). File- and folder-level grants (§4.1) extend
this module at M2, they do not replace it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import WorkspaceRole
from app.models.identity import User, Workspace, WorkspaceMember


class PermissionError_(Exception):
    """Base for access failures."""


class NotAMember(PermissionError_):
    """Requester has no membership in the workspace.

    Deliberately indistinguishable from "workspace does not exist" at the API layer:
    both surface as 404, so a non-member cannot probe which workspace IDs are real.
    """


class InsufficientRole(PermissionError_):
    def __init__(self, required: WorkspaceRole, actual: WorkspaceRole) -> None:
        super().__init__(f"Requires {required.value}; you are {actual.value}.")
        self.required = required
        self.actual = actual


class LastOwnerError(PermissionError_):
    """A workspace must always retain at least one owner (Architecture.md §4.6).

    Without this, demoting or removing the final owner orphans the workspace: nobody
    can invite, change roles, or delete it, ever again.
    """


async def get_membership(
    db: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember | None:
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def resolve_workspace_access(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    user: User,
    *,
    required: WorkspaceRole = WorkspaceRole.VIEWER,
) -> tuple[Workspace, WorkspaceRole]:
    """The single entry point for workspace access.

    Raises NotAMember if the workspace does not exist OR the user is not a member —
    the two are intentionally conflated so membership cannot be probed by ID.
    """
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise NotAMember("Workspace not found.")

    membership = await get_membership(db, workspace_id, user.id)
    if membership is None:
        raise NotAMember("Workspace not found.")

    # Workspace owner is not a bypass — it resolves through the same comparison, it
    # simply loses to nothing.
    if not membership.role.can_act_as(required):
        raise InsufficientRole(required, membership.role)

    return workspace, membership.role


async def count_owners(db: AsyncSession, workspace_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
        )
    )
    return result.scalar_one()


async def assert_not_last_owner(
    db: AsyncSession, workspace_id: uuid.UUID, member: WorkspaceMember
) -> None:
    """Guard demotion and removal of the final owner."""
    if member.role is not WorkspaceRole.OWNER:
        return
    if await count_owners(db, workspace_id) <= 1:
        raise LastOwnerError(
            "This is the workspace's only owner. Promote another member to owner first."
        )


async def change_member_role(
    db: AsyncSession,
    workspace: Workspace,
    member: WorkspaceMember,
    new_role: WorkspaceRole,
) -> WorkspaceMember:
    if member.role is new_role:
        return member
    if member.role is WorkspaceRole.OWNER:
        await assert_not_last_owner(db, workspace.id, member)

    member.role = new_role
    await db.flush()
    await _sync_owner_pointer(db, workspace)
    return member


async def remove_member(
    db: AsyncSession, workspace: Workspace, member: WorkspaceMember
) -> None:
    if workspace.is_personal and member.user_id == workspace.owner_id:
        # Schema.md §2: a personal workspace belongs to its user for as long as they
        # exist. Others may still be invited in; only the owner is immovable.
        raise LastOwnerError("The owner of a personal workspace cannot be removed.")
    await assert_not_last_owner(db, workspace.id, member)
    await db.delete(member)
    await db.flush()
    await _sync_owner_pointer(db, workspace)


async def _sync_owner_pointer(db: AsyncSession, workspace: Workspace) -> None:
    """Keep `workspaces.owner_id` pointing at an actual owner (Schema.md §2, v1.2).

    The column is display-only and never decides access, but leaving it pointing at a
    demoted or removed user makes the UI lie about who administers the workspace.
    """
    result = await db.execute(
        select(WorkspaceMember)
        .where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
        )
        .order_by(WorkspaceMember.created_at)
    )
    owners = result.scalars().all()
    if not owners:
        # Unreachable while the last-owner guard holds; assert rather than silently
        # leave a stale pointer, so a future change that breaks the invariant is loud.
        raise LastOwnerError("Workspace would be left with no owner.")
    if workspace.owner_id not in {owner.user_id for owner in owners}:
        workspace.owner_id = owners[0].user_id
        await db.flush()
