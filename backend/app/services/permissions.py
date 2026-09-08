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
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.content import FilePermission, Folder
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


# --- Object-level resolution (Architecture.md §4.1, added in M2) ------------------
#
# Precedence, first match wins, more specific always beats less specific:
#   1. explicit grant on the file itself
#   2. explicit grant on the nearest ancestor folder
#   3. the requester's workspace_members role
#   4. no membership -> denied
#
# Most-permissive-wins is explicitly rejected: it would make a deliberately narrowing
# grant silently ineffective, which is a security failure mode, not a convenience.

MAX_FOLDER_DEPTH = 32


class DepthLimitExceeded(PermissionError_):
    """Nesting beyond MAX_FOLDER_DEPTH.

    Every object-level permission check walks the ancestor chain, so unbounded depth
    means unbounded cost on the hottest path in the system.
    """


class CycleError(PermissionError_):
    """A move that would place a folder inside its own subtree.

    A cycle makes the ancestor walk non-terminating, so this guard protects the
    permission path itself, not merely the tidiness of the tree.
    """


_ANCESTOR_CTE = sa_text(
    """
    WITH RECURSIVE chain AS (
        SELECT id, parent_folder_id, workspace_id, deleted_at, 0 AS depth
        FROM folders
        WHERE id = :folder_id
        UNION ALL
        SELECT f.id, f.parent_folder_id, f.workspace_id, f.deleted_at, chain.depth + 1
        FROM folders f
        JOIN chain ON f.id = chain.parent_folder_id
        WHERE chain.depth < :max_depth
    )
    SELECT id, depth FROM chain ORDER BY depth
    """
)


async def ancestor_chain(
    db: AsyncSession, folder_id: uuid.UUID, *, include_deleted: bool = False
) -> list[uuid.UUID]:
    """Folder ids from the folder itself outward to the root, nearest first.

    One recursive CTE, not a query per level: a folder ten deep must not cost ten
    round trips on every file access.
    """
    result = await db.execute(
        _ANCESTOR_CTE, {"folder_id": folder_id, "max_depth": MAX_FOLDER_DEPTH}
    )
    rows = result.all()
    if not include_deleted:
        return [row.id for row in rows]
    return [row.id for row in rows]


async def resolve_object_role(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user: User,
    file_id: uuid.UUID | None = None,
    folder_id: uuid.UUID | None = None,
) -> WorkspaceRole | None:
    """The single object-level resolution path. Returns None when access is denied.

    `folder_id` is the file's containing folder when resolving a file, or the folder
    itself when resolving a folder.
    """
    membership = await get_membership(db, workspace_id, user.id)

    # 1. Explicit grant on the file itself.
    if file_id is not None:
        result = await db.execute(
            select(FilePermission.role).where(
                FilePermission.file_id == file_id,
                FilePermission.user_id == user.id,
            )
        )
        direct = result.scalar_one_or_none()
        if direct is not None:
            return direct

    # 2. Explicit grant on the nearest ancestor folder. A grant on a soft-deleted
    #    folder confers nothing.
    if folder_id is not None:
        chain = await ancestor_chain(db, folder_id)
        if chain:
            result = await db.execute(
                select(FilePermission.folder_id, FilePermission.role)
                .join(Folder, Folder.id == FilePermission.folder_id)
                .where(
                    FilePermission.folder_id.in_(chain),
                    FilePermission.user_id == user.id,
                    Folder.deleted_at.is_(None),
                )
            )
            grants = {row.folder_id: row.role for row in result.all()}
            if grants:
                # Nearest ancestor wins, including when it is LESS permissive.
                for candidate in chain:
                    if candidate in grants:
                        return grants[candidate]

    # 3. Workspace-level default.
    if membership is not None:
        return membership.role

    # 4. Not a member and no explicit grant.
    return None


async def require_object_role(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user: User,
    required: WorkspaceRole,
    file_id: uuid.UUID | None = None,
    folder_id: uuid.UUID | None = None,
) -> WorkspaceRole:
    role = await resolve_object_role(
        db, workspace_id=workspace_id, user=user, file_id=file_id, folder_id=folder_id
    )
    if role is None:
        raise NotAMember("Not found.")
    if not role.can_act_as(required):
        raise InsufficientRole(required, role)
    return role


async def assert_move_is_acyclic(
    db: AsyncSession, folder_id: uuid.UUID, new_parent_id: uuid.UUID | None
) -> None:
    """Refuse a move that would put a folder inside its own subtree."""
    if new_parent_id is None:
        return
    if new_parent_id == folder_id:
        raise CycleError("A folder cannot be moved into itself.")
    chain = await ancestor_chain(db, new_parent_id)
    if folder_id in chain:
        raise CycleError("A folder cannot be moved into one of its own descendants.")
    if len(chain) >= MAX_FOLDER_DEPTH:
        raise DepthLimitExceeded(
            f"Folders may not nest deeper than {MAX_FOLDER_DEPTH} levels."
        )
