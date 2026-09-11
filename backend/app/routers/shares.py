"""Share link endpoints (Architecture.md §4.2-§4.5).

Two routers, because they have opposite trust models:

- `owner_router` sits under the workspace prefix and requires membership like every
  other file route.
- `public_router` requires nothing at all. It is the only unauthenticated path to file
  content in the system, so everything on it is rate-limited, audited, and written to
  reveal as little as possible while still being usable.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.core import rate_limit
from app.core.config import settings
from app.core.dependencies import CurrentUser, DbSession, OptionalUser, ViewerAccess
from app.models.content import File, ShareLink
from app.models.enums import AuditAction, AuditTargetType, WorkspaceRole
from app.models.identity import User
from app.schemas.content import (
    ShareDownloadRequest,
    ShareDownloadResponse,
    ShareLinkCreate,
    ShareLinkCreated,
    ShareLinkOut,
    SharePreview,
)
from app.services import audit, notifications, permissions, shares, storage
from app.services import files as file_service
from app.tasks import notification_tasks

logger = logging.getLogger(__name__)

owner_router = APIRouter(
    prefix=f"{settings.api_v1_prefix}/workspaces/{{workspace_id}}",
    tags=["shares"],
)
public_router = APIRouter(prefix=f"{settings.api_v1_prefix}/shares", tags=["shares"])


# --- Owner side ------------------------------------------------------------------


def _out(link: ShareLink) -> ShareLinkOut:
    return ShareLinkOut(
        id=link.id,
        file_id=link.file_id,
        link_type=link.link_type,
        has_password=link.hashed_password is not None,
        allowed_user_id=link.allowed_user_id,
        expires_at=link.expires_at,
        max_downloads=link.max_downloads,
        uses=link.download_count,
        revoked_at=link.revoked_at,
        is_active=link.revoked_at is None,
        created_by=link.created_by,
        created_at=link.created_at,
    )


async def _load_file(db, workspace_id: uuid.UUID, file_id: uuid.UUID) -> File:
    file = await file_service.get_file(db, workspace_id, file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")
    return file


async def _require_editor(db, workspace, user: User, file: File) -> None:
    """Sharing requires EDITOR, not OWNER, and that is a considered choice.

    An editor can already download the file and forward it by any means they like, so
    withholding the share button from them would not protect the data — it would only
    make the audit trail worse by pushing the same act outside the system. What an
    editor cannot do is override the scanner (M4, owner-only), which is the decision
    that actually needs restricting.
    """
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.EDITOR,
            file_id=file.id,
            folder_id=file.folder_id,
        )
    except permissions.NotAMember as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from exc
    except permissions.PermissionError_ as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


@owner_router.post(
    "/files/{file_id}/shares",
    response_model=ShareLinkCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_share(
    file_id: uuid.UUID,
    payload: ShareLinkCreate,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> ShareLinkCreated:
    """Create a share link. **The token in this response is shown once and never again.**

    Refuses outright when the file's current version is not shareable, rather than
    handing back a link that would turn every visitor away (Design.md §5).
    """
    workspace, _ = access
    file = await _load_file(db, workspace.id, file_id)
    await _require_editor(db, workspace, user, file)

    version = await file_service.current_version(db, file)
    if version is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This file has no completed upload to share yet."
        )
    if not shares.version_is_shareable(version):
        raise HTTPException(
            status.HTTP_409_CONFLICT, shares.share_refusal_reason(version)
        )

    allowed_user_id = None
    if payload.allowed_user_email:
        found = (
            await db.execute(
                select(User).where(User.email == payload.allowed_user_email.lower())
            )
        ).scalar_one_or_none()
        if found is None:
            # Deliberately explicit: a private link is useless if the named person has
            # no account, and silently creating one nobody can open is worse.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "No account exists for that email, so a private link for them could "
                "not be opened by anyone.",
            )
        allowed_user_id = found.id

    try:
        link, raw_token = await shares.create(
            db,
            file=file,
            created_by=user,
            link_type=payload.link_type,
            password=payload.password,
            expires_at=payload.expires_at,
            max_downloads=payload.max_downloads,
            allowed_user_id=allowed_user_id,
        )
    except shares.InvalidShareRequest as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    await audit.record(
        db,
        action=AuditAction.SHARE_CREATED,
        target_type=AuditTargetType.SHARE_LINK,
        target_id=link.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": file.name,
            "file_id": str(file.id),
            "file_name": file.name,
            "link_type": link.link_type.value,
            "has_password": link.hashed_password is not None,
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
            "max_downloads": link.max_downloads,
        },
    )

    # Only a private link has anyone to notify (Architecture.md §5a). The other three
    # types are addressed to nobody in particular, and emailing the person who just
    # pressed "create link" would be noise. The token is deliberately not in the email.
    notified = await notifications.notify_file_shared(
        db, link=link, file=file, workspace=workspace, sharer=user
    )
    await db.flush()
    queued = [notified.id] if notified is not None else []
    await db.commit()
    await db.refresh(link)
    notification_tasks.dispatch(queued)

    return ShareLinkCreated(
        **_out(link).model_dump(),
        token=raw_token,
        url_path=f"/s/{raw_token}",
    )


@owner_router.get("/files/{file_id}/shares", response_model=list[ShareLinkOut])
async def list_shares(
    file_id: uuid.UUID, access: ViewerAccess, user: CurrentUser, db: DbSession
) -> list[ShareLinkOut]:
    """Links on a file. Viewers may see that a file is shared; only editors may change it."""
    workspace, _ = access
    file = await _load_file(db, workspace.id, file_id)
    try:
        await permissions.require_object_role(
            db,
            workspace_id=workspace.id,
            user=user,
            required=WorkspaceRole.VIEWER,
            file_id=file.id,
            folder_id=file.folder_id,
        )
    except permissions.PermissionError_ as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from exc
    return [_out(link) for link in await shares.for_file(db, file.id)]


@owner_router.delete("/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    share_id: uuid.UUID,
    request: Request,
    access: ViewerAccess,
    user: CurrentUser,
    db: DbSession,
) -> Response:
    """Revoke a link.

    Note what this does not do: a signed URL already handed out stays valid until it
    expires. That window is why share URLs use a 60-second TTL (Architecture.md §4.3).
    """
    workspace, _ = access
    link = await db.get(ShareLink, share_id)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found.")
    # Loaded through the workspace so a link id from another workspace cannot be
    # revoked by pairing it with a workspace this user belongs to.
    file = await file_service.get_file(db, workspace.id, link.file_id)
    if file is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share link not found.")
    await _require_editor(db, workspace, user, file)

    await shares.revoke(db, link)
    await audit.record(
        db,
        action=AuditAction.SHARE_REVOKED,
        target_type=AuditTargetType.SHARE_LINK,
        target_id=link.id,
        workspace_id=workspace.id,
        actor_id=user.id,
        request=request,
        metadata={
            "label": file.name,
            "file_id": str(file.id),
            "file_name": file.name,
        },
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Public side -----------------------------------------------------------------

_DENIAL_STATUS = {
    # Gone, not "forbidden": the link was real and is finished. A recipient can act on
    # that (ask for a new one) in a way that a blanket 403 does not allow.
    shares.Denial.REVOKED: status.HTTP_410_GONE,
    shares.Denial.EXPIRED: status.HTTP_410_GONE,
    shares.Denial.LIMIT_REACHED: status.HTTP_410_GONE,
    shares.Denial.FILE_GONE: status.HTTP_410_GONE,
    shares.Denial.PASSWORD_REQUIRED: status.HTTP_403_FORBIDDEN,
    shares.Denial.PASSWORD_INCORRECT: status.HTTP_403_FORBIDDEN,
    shares.Denial.SIGN_IN_REQUIRED: status.HTTP_401_UNAUTHORIZED,
    shares.Denial.NOT_THE_INVITED_USER: status.HTTP_403_FORBIDDEN,
    shares.Denial.WITHHELD_BY_SCAN: status.HTTP_403_FORBIDDEN,
}

_DENIAL_MESSAGE = {
    shares.Denial.UNKNOWN_TOKEN: "This link is not valid.",
    shares.Denial.REVOKED: "This link has been revoked by its owner.",
    shares.Denial.EXPIRED: "This link has expired.",
    shares.Denial.LIMIT_REACHED: "This link has reached its limit of uses.",
    shares.Denial.PASSWORD_REQUIRED: "This link needs a password.",
    shares.Denial.PASSWORD_INCORRECT: "That password is not correct.",
    shares.Denial.SIGN_IN_REQUIRED: "This link is for one specific person. Sign in to open it.",
    shares.Denial.NOT_THE_INVITED_USER: "This link was issued to a different person.",
    shares.Denial.FILE_GONE: "The file behind this link is no longer available.",
    shares.Denial.WITHHELD_BY_SCAN: (
        "The file behind this link is withheld pending its malware scan result."
    ),
}


async def _throttle(request: Request, token: str) -> None:
    """Per-IP ceiling on token attempts (Architecture.md §4.4).

    An unguessable token is only unguessable if you cannot try millions of them. Keyed
    on the client IP rather than the token, because the token is what an attacker
    varies.
    """
    client_ip = audit.client_ip(request) or "unknown"
    allowed, _ = await rate_limit.hit(
        f"share:ip:{client_ip}",
        limit=settings.share_attempts_per_ip_per_minute,
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Wait a minute and try again.",
        )


async def _audit_attempt(
    db,
    request: Request,
    resolution: shares.Resolution,
    requester: User | None,
    *,
    downloaded: bool,
) -> None:
    """Architecture.md §4.5.

    An unknown token writes nothing. There is no workspace to attribute it to and no
    target that resolves, and the volume is entirely attacker-controlled — brute-forcing
    would otherwise append unbounded rows to our own audit table, with a fail-open
    limiter as the only bound. Those attempts live in the rate limiter and this log line.
    """
    link = resolution.link
    if link is None:
        logger.info(
            "share token did not match any link (ip=%s)", audit.client_ip(request)
        )
        return

    file = resolution.file
    await audit.record(
        db,
        action=AuditAction.SHARE_ACCESSED if downloaded else AuditAction.SHARE_ACCESS_DENIED,
        target_type=AuditTargetType.SHARE_LINK,
        target_id=link.id,
        workspace_id=file.workspace_id if file is not None else None,
        # NULL for an anonymous visitor, which is exactly what Schema §10 expects here.
        actor_id=requester.id if requester is not None else None,
        request=request,
        metadata={
            # The file's name at the time. A denied attempt on a deleted file still
            # needs to say what was asked for, and by then the row may be gone.
            "label": file.name if file is not None else None,
            "file_id": str(link.file_id),
            "link_type": link.link_type.value,
            "denial_reason": resolution.denial.value if resolution.denial else None,
            "uses": link.download_count,
        },
    )


def _refuse(resolution: shares.Resolution) -> HTTPException:
    denial = resolution.denial
    return HTTPException(
        _DENIAL_STATUS.get(denial, status.HTTP_404_NOT_FOUND),
        _DENIAL_MESSAGE.get(denial, "This link is not valid."),
    )


@public_router.get("/{token}", response_model=SharePreview)
async def preview_share(
    token: str, request: Request, requester: OptionalUser, db: DbSession
) -> SharePreview:
    """What a visitor needs to decide what to do next, and nothing more.

    Runs the same checks as the download so a preview can never say yes to something the
    download refuses, but consumes no use from the limit — looking at a password prompt
    has not spent anything.

    A password gate withholds the file name. Sending the link and the password by
    separate channels is the entire point of password protection, so whoever holds only
    the link learns only that a password is needed.
    """
    await _throttle(request, token)
    resolution = await shares.resolve(
        db, raw_token=token, requester=requester, password=None
    )

    gated = resolution.denial in (
        shares.Denial.PASSWORD_REQUIRED,
        shares.Denial.SIGN_IN_REQUIRED,
    )
    if resolution.denial is not None and not gated:
        await _audit_attempt(db, request, resolution, requester, downloaded=False)
        await db.commit()
        raise _refuse(resolution)

    link = resolution.link
    if gated:
        # Not an audited denial: nothing was attempted yet, the visitor is being told
        # what the link needs. Auditing this would bury real failures in noise.
        return SharePreview(
            requires_password=resolution.denial is shares.Denial.PASSWORD_REQUIRED,
            requires_sign_in=resolution.denial is shares.Denial.SIGN_IN_REQUIRED,
            expires_at=link.expires_at if link else None,
        )

    return SharePreview(
        requires_password=False,
        requires_sign_in=False,
        expires_at=link.expires_at if link else None,
        file_name=resolution.file.name if resolution.file else None,
        size_bytes=resolution.version.size_bytes if resolution.version else None,
    )


@public_router.post("/{token}/download", response_model=ShareDownloadResponse)
async def redeem_share(
    token: str,
    payload: ShareDownloadRequest,
    request: Request,
    requester: OptionalUser,
    db: DbSession,
) -> ShareDownloadResponse:
    """Redeem a link for a short-lived, download-only URL."""
    await _throttle(request, token)
    resolution = await shares.resolve(
        db, raw_token=token, requester=requester, password=payload.password, for_download=True
    )

    if resolution.denial is not None:
        # A wrong password is throttled per LINK as well as per IP, so one link under
        # attack cannot be brute-forced and cannot exhaust an unrelated visitor's IP
        # budget either (Architecture.md §4.4).
        if resolution.denial is shares.Denial.PASSWORD_INCORRECT and resolution.link:
            allowed, _ = await rate_limit.hit(
                f"share:pw:{resolution.link.id}",
                limit=settings.share_password_attempts_per_link_per_minute,
                window_seconds=60,
            )
            if not allowed:
                await _audit_attempt(db, request, resolution, requester, downloaded=False)
                await db.commit()
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many password attempts on this link. Wait a minute.",
                )
        await _audit_attempt(db, request, resolution, requester, downloaded=False)
        await db.commit()
        raise _refuse(resolution)

    link = resolution.link
    file = resolution.file
    version = resolution.version
    try:
        link = await shares.consume(db, link)
    except shares.ShareError as exc:
        # Lost a race for the last use between resolution and the lock.
        await db.rollback()
        raise HTTPException(status.HTTP_410_GONE, str(exc)) from exc

    url = storage.generate_download_url(
        version.storage_key,
        download_filename=file.name,
        ttl_seconds=shares.share_url_ttl_seconds(),
    )
    await _audit_attempt(db, request, resolution, requester, downloaded=True)
    await db.commit()

    remaining = (
        None if link.max_downloads is None else max(0, link.max_downloads - link.download_count)
    )
    return ShareDownloadResponse(
        url=url,
        expires_in_seconds=shares.share_url_ttl_seconds(),
        file_name=file.name,
        size_bytes=version.size_bytes,
        uses_remaining=remaining,
    )
