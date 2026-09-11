"""Notification queueing, recipient resolution and rendering (Architecture.md §5).

Delivery itself lives in `tasks/notification_tasks.py`; the provider client is
`resend.py`. The split matters because queueing is transactional and sending cannot be:
a row is added to the transaction that caused the event, so an action that rolls back
cannot leave behind a notification claiming it happened — the same rule `audit.record`
follows — while the send happens afterwards, outside it.

Recipient resolution is here rather than at the call sites because for three of the five
event types the answer is not obvious, and scattering it would let two call sites
disagree. Architecture §5a is the specification; each function below cites the part of
it that decides.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.content import File, FileVersion, Folder, ShareLink
from app.models.enums import (
    NotificationType,
    ShareLinkType,
    WorkspaceRole,
)
from app.models.identity import Notification, User, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)

# Links in rendered bodies. Our own URLs only ever contain [A-Za-z0-9_\-./:#=%?],
# so stopping at whitespace is exact rather than heuristic.
_URL = re.compile(r"https?://[^\s<]+")


@dataclass(frozen=True)
class Rendered:
    """One email, ready to hand to the provider."""

    subject: str
    text: str
    html: str


async def queue(
    db: AsyncSession,
    *,
    type: NotificationType,
    email: str,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Notification:
    """Append one pending notification.

    Deliberately does NOT commit, for the same reason `audit.record` does not: the row
    belongs to the caller's transaction. Delivery is dispatched after that commits.
    """
    notification = Notification(
        user_id=user_id,
        email=email,
        workspace_id=workspace_id,
        type=type,
        payload=payload or {},
    )
    db.add(notification)
    return notification


async def _workspace_owner_users(
    db: AsyncSession, workspace_id: uuid.UUID
) -> list[User]:
    """Every owner of a workspace.

    Plural because §4.6's "a workspace always has at least one owner" invariant permits
    more than one, and a notification that only reaches the first one found would be a
    silent failure for the rest.
    """
    result = await db.execute(
        select(User)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
        )
    )
    return list(result.scalars().all())


# --- Triggers (Architecture.md §5a) -----------------------------------------------


async def notify_workspace_invitation(
    db: AsyncSession,
    *,
    email: str,
    raw_token: str,
    workspace: Workspace,
    inviter: User,
    role: WorkspaceRole,
) -> Notification:
    """The invited address.

    This is the one notification that carries a token (Rules.md §3): the token *is* the
    invitation, and the recipient may have no account through which to fetch it. M1
    returned it in the API response as an explicit stopgap; this replaces that.

    No `user_id` — the whole point of an invitation is that the person may not have an
    account yet, which is why Schema §11 made that column nullable at M6.
    """
    invitee = await db.execute(select(User).where(User.email == email))
    user = invitee.scalar_one_or_none()
    return await queue(
        db,
        type=NotificationType.WORKSPACE_INVITATION,
        email=email,
        user_id=user.id if user else None,
        workspace_id=workspace.id,
        payload={
            "workspace_name": workspace.name,
            "inviter_name": inviter.full_name or inviter.email,
            "role": role.value,
            "token": raw_token,
        },
    )


async def notify_file_shared(
    db: AsyncSession, *, link: ShareLink, file: File, workspace: Workspace, sharer: User
) -> Notification | None:
    """Only a **private** link has a recipient to notify (§5a).

    Public, password-protected and expiring links are addressed to nobody in
    particular. Emailing the person who just pressed "create link" would be noise
    dressed as a feature, so those three return None rather than inventing a recipient.

    The link's token is deliberately absent from the payload: the recipient signs in and
    the application resolves it. An emailed token would survive in a mailbox long after
    the link was revoked.
    """
    if link.link_type is not ShareLinkType.PRIVATE or not link.allowed_user_id:
        return None

    # A private link stores `allowed_user_id`, not an address: M5 refuses to create one
    # for an email with no account, precisely so the recipient always resolves here.
    user = await db.get(User, link.allowed_user_id)
    if user is None:
        return None
    return await queue(
        db,
        type=NotificationType.FILE_SHARED,
        email=user.email,
        user_id=user.id,
        workspace_id=workspace.id,
        payload={
            "file_name": file.name,
            "workspace_name": workspace.name,
            "sharer_name": sharer.full_name or sharer.email,
            "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        },
    )


async def notify_scan_flagged(
    db: AsyncSession, *, file: File, version: FileVersion, workspace: Workspace
) -> list[Notification]:
    """The uploader **and** every workspace owner, deduplicated (§5a).

    Both halves are load-bearing. The uploader needs to know their file is being
    withheld, or they see it vanish with no explanation. The owners need to know because
    §3d makes them the only people who can release it — notifying only the uploader
    leaves the one person who can act unaware.
    """
    owners = await _workspace_owner_users(db, workspace.id)
    recipients: dict[uuid.UUID, User] = {owner.id: owner for owner in owners}

    if version.uploaded_by:
        uploader = await db.get(User, version.uploaded_by)
        if uploader is not None:
            recipients.setdefault(uploader.id, uploader)

    payload = {
        "file_name": file.name,
        "workspace_name": workspace.name,
        "version_number": version.version_number,
        "detail": (version.scan_detail or {}).get("stats"),
    }
    return [
        await queue(
            db,
            type=NotificationType.SCAN_FLAGGED,
            email=user.email,
            user_id=user.id,
            workspace_id=workspace.id,
            payload=payload,
        )
        for user in recipients.values()
    ]


async def notify_permission_granted(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    role: WorkspaceRole,
    workspace: Workspace,
    target: File | Folder,
    granted_by: User,
) -> Notification | None:
    """The user who gained access (§5a).

    Only on a grant or a raise. Being told you have lost access is a different message
    with different wording, and v1 does not send it — silently emailing "your access
    changed" for a revocation would be worse than saying nothing.
    """
    user = await db.get(User, user_id)
    if user is None:
        return None
    return await queue(
        db,
        type=NotificationType.PERMISSION_GRANTED,
        email=user.email,
        user_id=user.id,
        workspace_id=workspace.id,
        payload={
            "target_name": target.name,
            "target_type": "folder" if isinstance(target, Folder) else "file",
            "role": role.value,
            "workspace_name": workspace.name,
            "granted_by": granted_by.full_name or granted_by.email,
        },
    )


async def notify_quota_warning(
    db: AsyncSession, *, workspace: Workspace, reserved_bytes: int
) -> list[Notification]:
    """Every workspace owner, on the **upward crossing** of the warning ratio (§5a).

    A warning is an edge, not a state. `storage_quota_warning_ratio` sat in the
    configuration from M0 with no reader at all, and the obvious wiring — notify while
    usage is at or above 90% — would email the owners on every upload for the rest of
    the workspace's life. So the condition is the transition: usage before the
    reservation below the threshold, usage after it at or above. Deleting files back
    under the threshold re-arms it, because the next crossing is a new edge.

    Called after `quota.reserve`, which has already added the bytes, so "before" is
    reconstructed by subtracting them rather than passed in and trusted.
    """
    quota = workspace.storage_quota_bytes
    if quota <= 0 or reserved_bytes <= 0:
        return []

    threshold = quota * settings.storage_quota_warning_ratio
    after = workspace.storage_used_bytes
    before = after - reserved_bytes
    if not (before < threshold <= after):
        return []

    owners = await _workspace_owner_users(db, workspace.id)
    payload = {
        "workspace_name": workspace.name,
        "used_bytes": after,
        "quota_bytes": quota,
        "percent_used": round(after / quota * 100, 1),
    }
    return [
        await queue(
            db,
            type=NotificationType.STORAGE_QUOTA_WARNING,
            email=owner.email,
            user_id=owner.id,
            workspace_id=workspace.id,
            payload=payload,
        )
        for owner in owners
    ]


# --- Rendering (Design.md §5) -----------------------------------------------------


def _app_url(path: str = "") -> str:
    return f"{settings.frontend_origin.rstrip('/')}{path}"


def _format_bytes(value: int) -> str:
    step = 1024.0
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < step or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= step
    return f"{size:.1f} GB"


def _body_for(notification: Notification) -> tuple[str, list[str]]:
    """Subject and paragraphs for one notification.

    Plain and textual by design: a security product writing to you, not a marketing
    send. Each message states the event that caused it and what, if anything, the
    reader should do (Design.md §5).

    No message carries a token or a signed URL — it links to the application and lets
    the app authenticate the reader. The sole exception is the invitation, where the
    token is the invitation itself and there is no other channel.
    """
    payload = notification.payload or {}
    workspace = payload.get("workspace_name", "a workspace")

    if notification.type is NotificationType.WORKSPACE_INVITATION:
        # The FRAGMENT, not the query string (Architecture.md §4.6). A fragment is the one
        # part of a URL no server receives, so neither the frontend server nor the VPS
        # reverse proxy can write the token into an access log. The first version of this
        # line used `?token=` — against the very rule §4.6 exists to state — and pointed
        # at a page that did not exist.
        url = _app_url(f"/invitations#token={quote(str(payload.get('token', '')), safe='')}")
        return (
            f"{payload.get('inviter_name', 'Someone')} invited you to {workspace}",
            [
                f"{payload.get('inviter_name', 'Someone')} has invited you to the "
                f"workspace “{workspace}” as {payload.get('role', 'a member')}.",
                f"Accept the invitation: {url}",
                "This link is specific to your email address. If you do not have an "
                "account yet, you will be asked to create one when you open it.",
            ],
        )

    if notification.type is NotificationType.FILE_SHARED:
        expires = payload.get("expires_at")
        paragraphs = [
            f"{payload.get('sharer_name', 'Someone')} shared the file "
            f"“{payload.get('file_name', 'a file')}” with you from {workspace}.",
            f"Sign in with this email address to open it: {_app_url('/files')}",
            "The link was issued to you specifically, so it will not work for anyone "
            "else, and it is not included in this email.",
        ]
        if expires:
            paragraphs.insert(1, f"Access expires on {expires}.")
        return (
            f"{payload.get('sharer_name', 'Someone')} shared "
            f"“{payload.get('file_name', 'a file')}” with you",
            paragraphs,
        )

    if notification.type is NotificationType.SCAN_FLAGGED:
        stats = payload.get("detail") or {}
        engines = stats.get("malicious")
        detected = (
            f" {engines} scanning engine{'s' if engines != 1 else ''} reported it as "
            "malicious."
            if isinstance(engines, int)
            else ""
        )
        return (
            f"“{payload.get('file_name', 'A file')}” was flagged by the malware scanner",
            [
                f"Version {payload.get('version_number', '?')} of "
                f"“{payload.get('file_name', 'a file')}” in {workspace} was "
                f"flagged by the malware scanner and is not downloadable.{detected}",
                "Nothing has been deleted. A workspace owner can release the file if "
                "they judge the detection to be a false positive, and that decision is "
                "recorded against the file with their stated reason.",
                f"Review it here: {_app_url('/files')}",
            ],
        )

    if notification.type is NotificationType.PERMISSION_GRANTED:
        return (
            f"You were given {payload.get('role', 'access')} on "
            f"“{payload.get('target_name', 'an item')}”",
            [
                f"{payload.get('granted_by', 'A workspace owner')} gave you "
                f"{payload.get('role', 'access')} access to the "
                f"{payload.get('target_type', 'item')} "
                f"“{payload.get('target_name', 'an item')}” in {workspace}.",
                f"Open it here: {_app_url('/files')}",
            ],
        )

    if notification.type is NotificationType.STORAGE_QUOTA_WARNING:
        used = payload.get("used_bytes", 0)
        quota = payload.get("quota_bytes", 0)
        return (
            f"{workspace} is at {payload.get('percent_used', '?')}% of its storage quota",
            [
                f"{workspace} has used {_format_bytes(int(used))} of its "
                f"{_format_bytes(int(quota))} quota "
                f"({payload.get('percent_used', '?')}%).",
                "Uploads are refused once the quota is reached, before any bytes move. "
                "Deleting files or older versions frees space immediately — every "
                "retained version counts towards the total.",
                f"Review the workspace: {_app_url('/files')}",
            ],
        )

    # Unreachable while the enum and this function agree. Raising rather than sending a
    # blank email: a notification nobody wrote wording for should fail loudly.
    raise ValueError(f"No wording defined for notification type {notification.type!r}")


def render(notification: Notification, *, redirected_from: str | None = None) -> Rendered:
    """Turn a queued row into an email.

    `redirected_from` produces the banner required by Design.md §5: while the sending
    domain is unverified the provider permits only one recipient, so development mail is
    diverted, and a test email that looks exactly like a real one is how a developer
    ends up believing a delivery path works when it has never been exercised.
    """
    subject, paragraphs = _body_for(notification)
    banner: str | None = None
    if redirected_from:
        banner = (
            f"[Redirected] This message was addressed to {redirected_from}. It was "
            "delivered here because the sending domain is not yet verified, and the "
            "provider's sandbox permits only one recipient."
        )
        subject = f"[for {redirected_from}] {subject}"

    text_parts = ([banner, ""] if banner else []) + paragraphs
    text = "\n\n".join(text_parts)

    escaped = [
        _URL.sub(
            # A real anchor, not bare text: whether bare text becomes clickable is up to
            # each mail client, and an invitation that cannot be opened is the defect
            # this milestone already shipped once. Applied after escaping, so the only
            # markup in a paragraph is the anchor this line adds.
            lambda m: f'<a href="{m.group(0)}" style="color:#1E3A5F">{m.group(0)}</a>',
            p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
        )
        for p in paragraphs
    ]
    html_banner = (
        f'<p style="margin:0 0 16px;padding:10px 12px;background:#FEF3C7;'
        f'border-left:3px solid #B45309;color:#7C2D12;font-size:13px">'
        f"{banner.replace('&', '&amp;').replace('<', '&lt;')}</p>"
        if banner
        else ""
    )
    body = "".join(
        f'<p style="margin:0 0 14px;line-height:1.55">{p}</p>' for p in escaped
    )
    html = (
        '<div style="font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:14px;color:#1F2937;max-width:560px">'
        f"{html_banner}{body}"
        '<p style="margin:24px 0 0;font-size:12px;color:#6B7280">'
        "Sent by your Secure File Sharing workspace because of the activity described "
        "above.</p></div>"
    )
    return Rendered(subject=subject, text=text, html=html)


def resolve_recipient(notification: Notification) -> tuple[str, str | None]:
    """Where this message actually goes, and who it was meant for.

    Returns `(to, redirected_from)`. `redirected_from` is None for a normal send, so a
    redirected message is always distinguishable from a real one on the row itself
    (Architecture §5c).
    """
    redirect = (settings.notification_redirect_to or "").strip()
    if redirect and redirect.lower() != notification.email.lower():
        return redirect, notification.email
    return notification.email, None


def utcnow() -> datetime:
    return datetime.now(UTC)
