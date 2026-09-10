"""Share links: creation, resolution, revocation (Architecture.md §4.2-§4.5).

This is the only access path in the system that does not begin with a workspace
membership check, which makes it the most security-sensitive code here. Three things are
therefore deliberate and stated rather than left to be inferred:

1. **The raw token exists only in the response to the request that created it.** What is
   persisted is SHA-256 of it (Rules.md §3). Nothing can hand a working link back later.
2. **Resolution never branches on `link_type` alone.** The type sets the base rule; the
   password, expiry and use limit are independent modifiers checked on every type.
3. **A scan override does not travel.** An owner releasing a flagged file for their own
   workspace is not the same act as publishing it to strangers, so sharing uses the
   stricter of the two rules — see `version_is_shareable`.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.models.content import File, FileVersion, ShareLink
from app.models.enums import ShareLinkType
from app.models.identity import User


class ShareError(Exception):
    """A refusal the caller turns into an HTTP status."""


class InvalidShareRequest(ShareError):
    """The requested link could not be built as asked."""


class Denial(enum.StrEnum):
    """Why a resolution attempt failed.

    Recorded in the audit metadata and mapped to a status code. Kept specific on
    purpose: "not available" would leave a legitimate recipient unable to tell an
    expired link from a file the scanner withheld, and those call for different actions.
    """

    # noqa on three members below: these name denial reasons written into an audit
    # log, not credentials. The linter matches on the word, not the meaning.
    UNKNOWN_TOKEN = "unknown_token"  # noqa: S105
    REVOKED = "revoked"
    EXPIRED = "expired"
    LIMIT_REACHED = "limit_reached"
    PASSWORD_REQUIRED = "password_required"  # noqa: S105
    PASSWORD_INCORRECT = "password_incorrect"  # noqa: S105
    SIGN_IN_REQUIRED = "sign_in_required"
    NOT_THE_INVITED_USER = "not_the_invited_user"
    FILE_GONE = "file_gone"
    WITHHELD_BY_SCAN = "withheld_by_scan"


@dataclass(frozen=True)
class Resolution:
    """The outcome of presenting a token.

    `link` is None only for an unknown token — the one case with nothing to audit
    against (Architecture.md §4.5).
    """

    link: ShareLink | None
    file: File | None
    version: FileVersion | None
    denial: Denial | None

    @property
    def granted(self) -> bool:
        return self.denial is None


# ------------------------------------------------------------------- the scan rule ----


def version_is_shareable(version: FileVersion) -> bool:
    """Whether a version may be served through a share link (Architecture.md §4.2).

    Stricter than `scan.version_is_downloadable` by exactly one thing: an admin override
    does not count. Expressed as the status-only property rather than as
    `downloadable and not has_override`, so the two rules are derived from the same
    source and cannot drift into disagreeing about `clean` or `scan_skipped`.

    The reasoning, because a stricter duplicate rule needs one: an override is a
    workspace owner accepting a risk for their own workspace. A share link hands the
    file to people who never saw the flag and never agreed to anything.
    """
    return version.scan_status.allows_download_without_override


# ---------------------------------------------------------------------- creation ----


async def create(
    db: AsyncSession,
    *,
    file: File,
    created_by: User,
    link_type: ShareLinkType,
    password: str | None = None,
    expires_at: datetime | None = None,
    max_downloads: int | None = None,
    allowed_user_id: uuid.UUID | None = None,
) -> tuple[ShareLink, str]:
    """Create a link and return it with the raw token, which is shown once.

    Validates the type requirements here as well as in the database. The CHECK
    constraint is the guarantee; this is what produces a message a user can act on
    instead of an integrity error.
    """
    if link_type is ShareLinkType.PASSWORD_PROTECTED and not password:
        raise InvalidShareRequest("A password-protected link needs a password.")
    if link_type is ShareLinkType.EXPIRING and expires_at is None:
        raise InvalidShareRequest("An expiring link needs an expiry time.")
    if link_type is ShareLinkType.PRIVATE and allowed_user_id is None:
        raise InvalidShareRequest("A private link needs the user it is for.")

    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise InvalidShareRequest("That expiry time has already passed.")
    if max_downloads is not None and max_downloads < 1:
        raise InvalidShareRequest("A use limit below 1 would make the link unusable.")

    raw_token, token_hash = security.generate_opaque_token()
    link = ShareLink(
        file_id=file.id,
        token_hash=token_hash,
        link_type=link_type,
        # Argon2 here, unlike the token: this one is a human-chosen secret.
        hashed_password=security.hash_password(password) if password else None,
        allowed_user_id=allowed_user_id,
        expires_at=expires_at,
        max_downloads=max_downloads,
        created_by=created_by.id,
    )
    db.add(link)
    await db.flush()
    return link, raw_token


async def for_file(db: AsyncSession, file_id: uuid.UUID) -> list[ShareLink]:
    """Links on one file, newest first. Never includes a token — there is none to give."""
    result = await db.execute(
        select(ShareLink)
        .where(ShareLink.file_id == file_id)
        .order_by(ShareLink.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke(db: AsyncSession, link: ShareLink) -> ShareLink:
    """Mark a link unusable.

    Not a delete: the row has to survive so `audit_logs` entries pointing at it still
    resolve, which is the same reasoning soft delete uses everywhere else (Schema.md).

    **Revocation is not retroactive over already-issued URLs.** A signed URL handed out
    moments ago stays valid until it expires, which is why share links use a 60-second
    TTL (Architecture.md §4.3). Recorded here so nobody reads `revoke` as "recall".
    """
    if link.revoked_at is None:
        link.revoked_at = datetime.now(UTC)
        await db.flush()
    return link


# -------------------------------------------------------------------- resolution ----


async def get_by_token(db: AsyncSession, raw_token: str) -> ShareLink | None:
    """Look a link up by the raw token, which is never stored."""
    if not raw_token:
        return None
    result = await db.execute(
        select(ShareLink).where(ShareLink.token_hash == security.hash_token(raw_token))
    )
    return result.scalar_one_or_none()


async def resolve(
    db: AsyncSession,
    *,
    raw_token: str,
    requester: User | None,
    password: str | None = None,
    for_download: bool = False,
) -> Resolution:
    """Evaluate a token against every rule in Architecture.md §4.2.

    Checks run cheapest-first and in an order chosen so that a caller learns the least
    surprising thing: whether the link is usable at all, then whether *they* may use it,
    then whether the file behind it can be served.

    `for_download=False` is the preview path. It runs the identical checks so a preview
    can never say yes to something the download would refuse, but it does not consume a
    use from the limit — a visitor looking at a password prompt has not spent anything.
    """
    link = await get_by_token(db, raw_token)
    if link is None:
        # The only outcome with no link to audit against (§4.5).
        return Resolution(None, None, None, Denial.UNKNOWN_TOKEN)

    file = await db.get(File, link.file_id)
    version = (
        await db.get(FileVersion, file.current_version_id)
        if file is not None and file.current_version_id is not None
        else None
    )

    def denied(reason: Denial) -> Resolution:
        return Resolution(link, file, version, reason)

    # 1. Is the link itself still usable? Applies to every type.
    if link.revoked_at is not None:
        return denied(Denial.REVOKED)
    if link.expires_at is not None and datetime.now(UTC) >= link.expires_at:
        return denied(Denial.EXPIRED)
    if link.max_downloads is not None and link.download_count >= link.max_downloads:
        return denied(Denial.LIMIT_REACHED)

    # 2. May this requester use it? `private` is the only type that asks who they are.
    if link.link_type is ShareLinkType.PRIVATE:
        if requester is None:
            return denied(Denial.SIGN_IN_REQUIRED)
        if requester.id != link.allowed_user_id:
            return denied(Denial.NOT_THE_INVITED_USER)

    # 3. A password gates any type that carries one, not only `password_protected`.
    if link.hashed_password is not None:
        if not password:
            return denied(Denial.PASSWORD_REQUIRED)
        # Constant-time via the password hasher (Architecture.md §4.2).
        if not security.verify_password(password, link.hashed_password):
            return denied(Denial.PASSWORD_INCORRECT)

    # 4. Is there anything to serve?
    if file is None or file.deleted_at is not None:
        return denied(Denial.FILE_GONE)
    if version is None or version.deleted_at is not None:
        return denied(Denial.FILE_GONE)
    if not version_is_shareable(version):
        return denied(Denial.WITHHELD_BY_SCAN)

    return Resolution(link, file, version, None)


async def consume(db: AsyncSession, link: ShareLink) -> ShareLink:
    """Count one use, under a row lock (Architecture.md §4.2).

    Without the lock, two requests arriving together at `max_downloads - 1` both read
    the old count and both succeed, so a "one use" link serves twice. The same reasoning
    as the quota reservation in §6, and the same mechanism.
    """
    locked = (
        await db.execute(
            select(ShareLink).where(ShareLink.id == link.id).with_for_update()
        )
    ).scalar_one()
    if locked.max_downloads is not None and locked.download_count >= locked.max_downloads:
        # Someone else took the last use between resolution and here.
        raise ShareError("This link has reached its use limit.")
    locked.download_count += 1
    await db.flush()
    return locked


def share_url_ttl_seconds() -> int:
    """Shorter than an authenticated download, because this is the revocation window."""
    return settings.signed_share_url_ttl_seconds
