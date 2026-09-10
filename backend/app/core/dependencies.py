"""FastAPI dependencies for authentication and workspace access.

These wrap `app.services.permissions` so every route resolves access the same way
(Architecture.md §4). A route must never query `workspace_members` for itself.


NOTE: this module deliberately does NOT use `from __future__ import annotations`.
FastAPI resolves dependency signatures at runtime, and a stringified nested
`Annotated[uuid.UUID, Path()]` inside `WorkspaceAccess.__call__` cannot be resolved.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.database import get_db
from app.models.enums import WorkspaceRole
from app.models.identity import User, Workspace
from app.services import permissions

CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
)


async def get_current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    token = request.cookies.get(security.ACCESS_COOKIE_NAME)
    if not token:
        raise CREDENTIALS_EXCEPTION
    try:
        user_id = security.decode_access_token(token)
    except security.TokenError as exc:
        raise CREDENTIALS_EXCEPTION from exc

    user = await db.get(User, user_id)
    if user is None:
        # Token is validly signed but the account is gone.
        raise CREDENTIALS_EXCEPTION
    return user


async def get_optional_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    """The signed-in user if there is one, otherwise None.

    Share links need this: a `private` link requires an identified user, while the other
    three types must work for someone with no account at all (Architecture.md §4.2). A
    bad or expired cookie resolves to None rather than raising, because on this path not
    being signed in is a legitimate state, not an error.
    """
    token = request.cookies.get(security.ACCESS_COOKIE_NAME)
    if not token:
        return None
    try:
        user_id = security.decode_access_token(token)
    except security.TokenError:
        return None
    return await db.get(User, user_id)


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def _translate(exc: permissions.PermissionError_) -> HTTPException:
    if isinstance(exc, permissions.NotAMember):
        # 404, not 403: a non-member must not be able to learn that a workspace ID
        # exists. Returning 403 here would make workspace IDs enumerable.
        return HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found.")
    if isinstance(exc, permissions.InsufficientRole):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


class WorkspaceAccess:
    """Dependency factory: require at least `required` in the path workspace."""

    def __init__(self, required: WorkspaceRole = WorkspaceRole.VIEWER) -> None:
        self.required = required

    async def __call__(
        self,
        workspace_id: Annotated[uuid.UUID, Path()],
        user: CurrentUser,
        db: DbSession,
    ) -> tuple[Workspace, WorkspaceRole]:
        try:
            return await permissions.resolve_workspace_access(
                db, workspace_id, user, required=self.required
            )
        except permissions.PermissionError_ as exc:
            raise _translate(exc) from exc


require_viewer = WorkspaceAccess(WorkspaceRole.VIEWER)
require_editor = WorkspaceAccess(WorkspaceRole.EDITOR)
require_owner = WorkspaceAccess(WorkspaceRole.OWNER)

ViewerAccess = Annotated[tuple[Workspace, WorkspaceRole], Depends(require_viewer)]
EditorAccess = Annotated[tuple[Workspace, WorkspaceRole], Depends(require_editor)]
OwnerAccess = Annotated[tuple[Workspace, WorkspaceRole], Depends(require_owner)]
