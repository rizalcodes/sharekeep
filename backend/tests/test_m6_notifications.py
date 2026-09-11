"""M6 notification pipeline (Architecture.md §5).

The provider is stubbed here. What is *not* stubbed is the decision-making: who receives
each event, whether a failure is retried, and whether a message can go out twice. Those
are the parts that would cost a real person a wrong or duplicated email, and the live
validation against Resend covers the parts only the provider can answer.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.enums import (
    NotificationStatus,
    NotificationType,
    ScanStatus,
    ShareLinkType,
    WorkspaceRole,
)
from app.models.identity import Notification, WorkspaceMember
from app.services import notifications, resend
from app.tasks import notification_tasks
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"
STRONG_PASSWORD = "a-long-share-password"


@pytest.fixture
def task_session(db, monkeypatch):
    """Lend the delivery task the test's session.

    The task opens its own via `SessionLocal`, which is right for a worker but invisible
    here: the test session lives inside a transaction that is always rolled back, so a
    second connection would find no rows and every delivery test would pass vacuously
    with "gone". Patching the factory keeps the task's real code path — including the
    `with_for_update` claim and the commit — running against data the test can see.
    """

    class _Lent:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            # Deliberately does not close: the fixture owns this session's lifetime.
            return False

    monkeypatch.setattr(notification_tasks, "SessionLocal", _Lent)
    return db


async def _workspace(make_client, email: str):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Notified"})).json()["id"]
    return client, ws, session


async def _queued(db, workspace_id: str, type_: NotificationType) -> list[Notification]:
    result = await db.execute(
        select(Notification).where(
            Notification.workspace_id == uuid.UUID(workspace_id),
            Notification.type == type_,
        )
    )
    return list(result.scalars().all())


# --- Who gets notified (Architecture.md §5a) -------------------------------------


async def test_an_invitation_queues_an_email_to_the_invited_address(make_client, db):
    """M1 returned the token in the API response as an explicit stopgap. This is the
    channel it was waiting for."""
    client, ws, _ = await _workspace(make_client, "m6n-inv@example.com")

    created = await client.post(
        f"{WS}/{ws}/invitations",
        json={"email": "newcomer@example.com", "role": WorkspaceRole.EDITOR.value},
    )
    assert created.status_code == 201

    rows = await _queued(db, ws, NotificationType.WORKSPACE_INVITATION)
    assert len(rows) == 1
    assert rows[0].email == "newcomer@example.com"
    # No account yet, which is exactly why Schema §11 made user_id nullable at M6.
    assert rows[0].user_id is None
    assert rows[0].status is NotificationStatus.PENDING


async def test_the_invitation_is_the_only_email_that_carries_a_token(make_client, db):
    """Rules.md §3: the token *is* the invitation and there is no other channel. Every
    other notification links to the app and lets it authenticate the reader."""
    client, ws, _ = await _workspace(make_client, "m6n-tok@example.com")
    await client.post(
        f"{WS}/{ws}/invitations",
        json={"email": "tokencheck@example.com", "role": WorkspaceRole.VIEWER.value},
    )
    row = (await _queued(db, ws, NotificationType.WORKSPACE_INVITATION))[0]
    assert row.payload.get("token")

    rendered = notifications.render(row)
    assert row.payload["token"] in rendered.text


async def test_a_private_share_notifies_the_named_recipient(make_client, db):
    client, ws, _ = await _workspace(make_client, "m6n-priv@example.com")
    recipient, recipient_session = await new_user(make_client, "m6n-rcpt@example.com")

    file_id = (await upload_file(client, ws, "for-you.pdf")).json()["id"]
    created = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={
            "link_type": ShareLinkType.PRIVATE.value,
            "allowed_user_email": "m6n-rcpt@example.com",
        },
    )
    assert created.status_code == 201, created.text

    rows = await _queued(db, ws, NotificationType.FILE_SHARED)
    assert len(rows) == 1
    assert rows[0].email == "m6n-rcpt@example.com"
    assert rows[0].user_id == uuid.UUID(recipient_session["user"]["id"])


@pytest.mark.parametrize(
    "link_type,extra",
    [
        (ShareLinkType.PUBLIC.value, {}),
        (ShareLinkType.PASSWORD_PROTECTED.value, {"password": STRONG_PASSWORD}),
        (ShareLinkType.EXPIRING.value, {"expires_at": "2099-01-01T00:00:00Z"}),
    ],
)
async def test_the_other_three_link_types_notify_nobody(make_client, db, link_type, extra):
    """They have no recipient. Emailing whoever just pressed "create link" would be
    noise dressed as a feature (§5a)."""
    client, ws, _ = await _workspace(make_client, f"m6n-{link_type}@example.com")
    file_id = (await upload_file(client, ws, "public.pdf")).json()["id"]

    created = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares", json={"link_type": link_type, **extra}
    )
    assert created.status_code == 201, created.text
    assert await _queued(db, ws, NotificationType.FILE_SHARED) == []


async def test_a_permission_grant_notifies_the_person_who_gained_access(make_client, db):
    client, ws, _ = await _workspace(make_client, "m6n-grant@example.com")
    other, other_session = await new_user(make_client, "m6n-gets@example.com")
    other_id = other_session["user"]["id"]

    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(other_id),
            role=WorkspaceRole.VIEWER,
        )
    )
    await db.commit()

    file_id = (await upload_file(client, ws, "shared.pdf")).json()["id"]
    granted = await client.post(
        f"{WS}/{ws}/files/{file_id}/grants",
        json={"user_id": other_id, "role": WorkspaceRole.EDITOR.value},
    )
    assert granted.status_code in (200, 201), granted.text

    rows = await _queued(db, ws, NotificationType.PERMISSION_GRANTED)
    assert len(rows) == 1
    assert rows[0].email == "m6n-gets@example.com"
    assert rows[0].payload["role"] == WorkspaceRole.EDITOR.value


async def test_lowering_a_role_notifies_nobody(make_client, db):
    """v1 notifies on gaining access, never on losing it (§5a). "Your access changed"
    with no explanation would be worse than silence."""
    client, ws, _ = await _workspace(make_client, "m6n-lower@example.com")
    other, other_session = await new_user(make_client, "m6n-loses@example.com")
    other_id = other_session["user"]["id"]

    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(other_id),
            role=WorkspaceRole.VIEWER,
        )
    )
    await db.commit()

    file_id = (await upload_file(client, ws, "shared.pdf")).json()["id"]
    grants = f"{WS}/{ws}/files/{file_id}/grants"
    await client.post(grants, json={"user_id": other_id, "role": WorkspaceRole.OWNER.value})
    before = len(await _queued(db, ws, NotificationType.PERMISSION_GRANTED))

    # Same user, lower role: a change, but not a grant.
    await client.post(grants, json={"user_id": other_id, "role": WorkspaceRole.VIEWER.value})
    after = len(await _queued(db, ws, NotificationType.PERMISSION_GRANTED))
    assert after == before


# --- The quota warning is an edge, not a state (§5a) -----------------------------


async def test_crossing_the_warning_threshold_notifies_the_owner_once(make_client, db):
    client, ws, _ = await _workspace(make_client, "m6n-quota@example.com")

    from app.models.identity import Workspace

    workspace = await db.get(Workspace, uuid.UUID(ws))
    workspace.storage_quota_bytes = 10_000
    await db.commit()

    # Well under the 90% threshold: no warning.
    assert (await upload_file(client, ws, "small.bin", size_bytes=1_000)).status_code == 200
    assert await _queued(db, ws, NotificationType.STORAGE_QUOTA_WARNING) == []

    # Crosses it: exactly one warning.
    assert (await upload_file(client, ws, "big.bin", size_bytes=8_500)).status_code == 200
    first = await _queued(db, ws, NotificationType.STORAGE_QUOTA_WARNING)
    assert len(first) == 1

    # Still above it. A state-based check would fire again here, which is the whole
    # reason §5a specifies an edge — otherwise every upload for the life of the
    # workspace emails the owners.
    assert (await upload_file(client, ws, "tiny.bin", size_bytes=100)).status_code == 200
    assert len(await _queued(db, ws, NotificationType.STORAGE_QUOTA_WARNING)) == 1


async def test_the_warning_re_arms_after_dropping_back_under(make_client, db):
    client, ws, _ = await _workspace(make_client, "m6n-rearm@example.com")

    from app.models.identity import Workspace

    workspace = await db.get(Workspace, uuid.UUID(ws))
    workspace.storage_quota_bytes = 10_000
    await db.commit()

    uploaded = await upload_file(client, ws, "big.bin", size_bytes=9_500)
    assert uploaded.status_code == 200
    assert len(await _queued(db, ws, NotificationType.STORAGE_QUOTA_WARNING)) == 1

    await client.delete(f"{WS}/{ws}/files/{uploaded.json()['id']}")
    await db.refresh(workspace)

    assert (await upload_file(client, ws, "again.bin", size_bytes=9_500)).status_code == 200
    assert len(await _queued(db, ws, NotificationType.STORAGE_QUOTA_WARNING)) == 2


# --- Rendering --------------------------------------------------------------------


async def test_every_notification_type_has_wording(db):
    """A type with no wording must fail loudly rather than send a blank email."""
    for type_ in NotificationType:
        row = Notification(
            email="reader@example.com",
            type=type_,
            payload={
                "workspace_name": "Acme",
                "file_name": "report.pdf",
                "target_name": "Reports",
                "inviter_name": "Dana",
                "sharer_name": "Dana",
                "granted_by": "Dana",
                "role": "editor",
                "token": "tok",
                "version_number": 2,
                "used_bytes": 900,
                "quota_bytes": 1000,
                "percent_used": 90.0,
            },
        )
        rendered = notifications.render(row)
        assert rendered.subject and rendered.text and rendered.html


async def test_an_unknown_type_raises_rather_than_sending_an_empty_message(db):
    row = Notification(email="a@example.com", type=NotificationType.FILE_SHARED, payload={})
    row.type = "not_a_real_type"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="No wording defined"):
        notifications.render(row)


async def test_a_redirected_message_says_so_in_the_subject_and_the_body(db):
    """Design.md §5: a test email that looks exactly like a real one is how a developer
    ends up believing a delivery path works when it has never been exercised."""
    row = Notification(
        email="intended@example.com",
        type=NotificationType.FILE_SHARED,
        payload={"file_name": "x.pdf", "sharer_name": "Dana", "workspace_name": "Acme"},
    )
    rendered = notifications.render(row, redirected_from="intended@example.com")
    assert "intended@example.com" in rendered.subject
    assert "Redirected" in rendered.text
    assert "intended@example.com" in rendered.html


async def test_the_recipient_is_redirected_only_when_configured(monkeypatch, db):
    row = Notification(email="real@example.com", type=NotificationType.FILE_SHARED, payload={})

    monkeypatch.setattr(settings, "notification_redirect_to", None)
    assert notifications.resolve_recipient(row) == ("real@example.com", None)

    monkeypatch.setattr(settings, "notification_redirect_to", "dev@example.com")
    assert notifications.resolve_recipient(row) == ("dev@example.com", "real@example.com")

    # Redirecting to the address it was already going to is not a redirect, and must
    # not be recorded as one.
    monkeypatch.setattr(settings, "notification_redirect_to", "real@example.com")
    assert notifications.resolve_recipient(row) == ("real@example.com", None)


async def test_no_notification_but_the_invitation_carries_a_share_token(make_client, db):
    """A share link's token must not travel by email: a mailbox outlives a revocation."""
    client, ws, _ = await _workspace(make_client, "m6n-notok@example.com")
    recipient, _ = await new_user(make_client, "m6n-r2@example.com")

    file_id = (await upload_file(client, ws, "secret.pdf")).json()["id"]
    created = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={
            "link_type": ShareLinkType.PRIVATE.value,
            "allowed_user_email": "m6n-r2@example.com",
        },
    )
    token = created.json()["token"]

    row = (await _queued(db, ws, NotificationType.FILE_SHARED))[0]
    rendered = notifications.render(row)
    assert token not in rendered.text
    assert token not in rendered.html
    assert "token" not in row.payload


# --- Delivery (Architecture.md §5b) ----------------------------------------------


class _Stub:
    """Stands in for `resend.send_email`, mirroring its signature so a change to the
    real one breaks this rather than passing silently — the M5 lesson about stubs."""

    def __init__(self, *, raises=None, message_id="msg-1"):
        self.raises = raises
        self.message_id = message_id
        self.calls: list[dict] = []

    async def __call__(self, *, to, subject, text, html=None, sender=None):
        self.calls.append(
            {"to": to, "subject": subject, "text": text, "html": html, "sender": sender}
        )
        if self.raises is not None:
            raise self.raises
        return resend.Accepted(provider_message_id=self.message_id)


async def _one_pending(db, make_client, email="m6n-deliver@example.com"):
    client, ws, _ = await _workspace(make_client, email)
    await client.post(
        f"{WS}/{ws}/invitations",
        json={"email": "target@example.com", "role": WorkspaceRole.VIEWER.value},
    )
    row = (await _queued(db, ws, NotificationType.WORKSPACE_INVITATION))[0]
    return client, ws, row


async def test_a_successful_send_records_the_providers_id_and_nothing_more(
    make_client, db, monkeypatch, task_session
):
    """`sent` means the provider accepted it. The restricted key cannot tell us more,
    and the row must not imply it can."""
    _, _, row = await _one_pending(db, make_client, "m6n-ok@example.com")
    stub = _Stub(message_id="resend-abc")
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)

    outcome = await notification_tasks._deliver(row.id)
    assert outcome == "sent"

    await db.refresh(row)
    assert row.status is NotificationStatus.SENT
    assert row.provider_message_id == "resend-abc"
    assert row.sent_at is not None
    assert row.redirected_to is None
    assert row.attempts == 1
    assert stub.calls[0]["to"] == "target@example.com"


async def test_a_row_already_sent_is_never_sent_again(
    make_client, db, monkeypatch, task_session
):
    """The guard that matters most: an email cannot be recalled, so a duplicate is not
    a retry — it is a second message to a real person."""
    _, _, row = await _one_pending(db, make_client, "m6n-idem@example.com")
    stub = _Stub()
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)

    assert await notification_tasks._deliver(row.id) == "sent"
    assert await notification_tasks._deliver(row.id) == "already_sent"
    assert len(stub.calls) == 1


async def test_a_permanent_refusal_is_recorded_and_not_retried(
    make_client, db, monkeypatch, task_session
):
    """The sandbox's owner-only 403 is permanent. Retrying it only burns free-tier
    budget against an answer that cannot change."""
    _, _, row = await _one_pending(db, make_client, "m6n-perm@example.com")
    refusal = resend.PermanentError(
        "HTTP 403 validation_error: You can only send testing emails to your own "
        "email address (owner@example.com)."
    )
    stub = _Stub(raises=refusal)
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)

    assert await notification_tasks._deliver(row.id) == "failed_permanent"

    await db.refresh(row)
    assert row.status is NotificationStatus.FAILED
    # The provider's own wording, quotable rather than paraphrased.
    assert "only send testing emails" in row.last_error
    assert row.sent_at is None

    # A failed row is terminal: a second attempt must not reach the provider.
    assert await notification_tasks._deliver(row.id) == "already_failed"
    assert len(stub.calls) == 1


async def test_a_transient_failure_raises_so_celery_retries_it(
    make_client, db, monkeypatch, task_session
):
    _, _, row = await _one_pending(db, make_client, "m6n-trans@example.com")
    stub = _Stub(raises=resend.TransientError("Resend is unavailable. HTTP 503"))
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)

    with pytest.raises(resend.TransientError):
        await notification_tasks._deliver(row.id)

    await db.refresh(row)
    # Still pending, with the attempt counted ON THE ROW: a worker restart must not
    # reset the count, which is the whole reason `attempts` is not left to Celery.
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 1
    assert "unavailable" in row.last_error


async def test_transient_failures_stop_after_the_attempt_ceiling(
    make_client, db, monkeypatch, task_session
):
    """Left pending forever, the sweep would retry it until the end of time and a reader
    would never learn why it never arrived."""
    _, _, row = await _one_pending(db, make_client, "m6n-exh@example.com")
    stub = _Stub(raises=resend.TransientError("timed out"))
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)
    monkeypatch.setattr(settings, "notification_max_attempts", 2)

    with pytest.raises(resend.TransientError):
        await notification_tasks._deliver(row.id)
    assert await notification_tasks._deliver(row.id) == "failed_exhausted"

    await db.refresh(row)
    assert row.status is NotificationStatus.FAILED
    assert row.attempts == 2


async def test_a_missing_key_leaves_the_row_pending_rather_than_failed(
    make_client, db, monkeypatch, task_session
):
    """Nothing is wrong with the notification, only with the configuration. Marking it
    failed would discard a message that becomes deliverable the moment a key is set."""
    _, _, row = await _one_pending(db, make_client, "m6n-nokey@example.com")
    monkeypatch.setattr(settings, "resend_api_key", None)

    assert await notification_tasks._deliver(row.id) == "not_configured"

    await db.refresh(row)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 0
    assert row.sent_at is None


async def test_a_redirected_send_is_recorded_as_redirected(
    make_client, db, monkeypatch, task_session
):
    _, _, row = await _one_pending(db, make_client, "m6n-redir@example.com")
    stub = _Stub()
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", "dev@example.com")

    assert await notification_tasks._deliver(row.id) == "sent"

    await db.refresh(row)
    assert row.redirected_to == "dev@example.com"
    # The row still records who it was addressed to, so a redirected send can never be
    # mistaken for a real one.
    assert row.email == "target@example.com"
    assert stub.calls[0]["to"] == "dev@example.com"


async def test_a_deleted_row_is_not_an_error(
    make_client, db, monkeypatch, task_session
):
    monkeypatch.setattr(resend, "send_email", _Stub())
    assert await notification_tasks._deliver(uuid.uuid4()) == "gone"


async def test_the_sweep_picks_up_pending_rows_and_skips_terminal_ones(
    make_client, db, monkeypatch, task_session
):
    _, _, pending = await _one_pending(db, make_client, "m6n-sweep@example.com")
    stub = _Stub()
    monkeypatch.setattr(resend, "send_email", stub)
    monkeypatch.setattr(settings, "notification_redirect_to", None)

    dispatched: list[str] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "delay",
        lambda notification_id: dispatched.append(notification_id),
    )

    result = await notification_tasks._sweep()
    assert result["dispatched"] >= 1
    assert str(pending.id) in dispatched

    # Once sent, the sweep must not pick it up again.
    await notification_tasks._deliver(pending.id)
    dispatched.clear()
    await notification_tasks._sweep()
    assert str(pending.id) not in dispatched


async def test_the_sweep_is_bounded(make_client, db, monkeypatch, task_session):
    """An unbounded sweep after an outage would hand the provider thousands of messages
    at once and hit the rate limit this design is careful to stay under."""
    client, ws, _ = await _workspace(make_client, "m6n-bound@example.com")
    for index in range(6):
        await client.post(
            f"{WS}/{ws}/invitations",
            json={"email": f"b{index}@example.com", "role": WorkspaceRole.VIEWER.value},
        )

    monkeypatch.setattr(settings, "notification_batch_size", 3)
    dispatched: list[str] = []
    monkeypatch.setattr(
        notification_tasks.deliver_notification,
        "delay",
        lambda notification_id: dispatched.append(notification_id),
    )

    result = await notification_tasks._sweep()
    assert result["dispatched"] == 3
    assert len(dispatched) == 3


# --- Queueing is transactional ---------------------------------------------------


async def test_a_flagged_scan_notifies_the_uploader_and_the_owner(make_client, db):
    """Both halves are load-bearing: the uploader learns their file is withheld, and the
    owners learn because §3d makes them the only people who can release it."""
    from app.models.content import File, FileVersion
    from app.models.identity import Workspace

    client, ws, session = await _workspace(make_client, "m6n-flag@example.com")
    uploaded = await upload_file(client, ws, "suspect.bin")
    file_id = uuid.UUID(uploaded.json()["id"])

    file = await db.get(File, file_id)
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    version.scan_detail = {"stats": {"malicious": 3}}
    await db.commit()

    workspace = await db.get(Workspace, uuid.UUID(ws))
    queued = await notifications.notify_scan_flagged(
        db, file=file, version=version, workspace=workspace
    )
    await db.commit()

    # Uploader and owner are the same person here, so dedup must leave exactly one.
    assert len(queued) == 1
    assert queued[0].email == "m6n-flag@example.com"
    assert queued[0].payload["detail"] == {"malicious": 3}


async def test_a_flagged_scan_notifies_a_separate_uploader_and_owner(make_client, db):
    from app.models.content import File, FileVersion
    from app.models.identity import Workspace

    owner, ws, _ = await _workspace(make_client, "m6n-fowner@example.com")
    editor, editor_session = await new_user(make_client, "m6n-feditor@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(editor_session["user"]["id"]),
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.commit()

    uploaded = await upload_file(editor, ws, "editor-upload.bin")
    file = await db.get(File, uuid.UUID(uploaded.json()["id"]))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_detail = {"stats": {"malicious": 1}}
    await db.commit()

    workspace = await db.get(Workspace, uuid.UUID(ws))
    queued = await notifications.notify_scan_flagged(
        db, file=file, version=version, workspace=workspace
    )
    await db.commit()

    addresses = {row.email for row in queued}
    assert addresses == {"m6n-fowner@example.com", "m6n-feditor@example.com"}


async def test_delivery_is_dispatched_only_after_the_transaction_commits(
    make_client, db, request
):
    """A worker must never see a row a rollback would have removed."""
    client, ws, _ = await _workspace(make_client, "m6n-dispatch@example.com")
    await client.post(
        f"{WS}/{ws}/invitations",
        json={"email": "dispatched@example.com", "role": WorkspaceRole.VIEWER.value},
    )

    dispatched = [
        call for call in request.node.dispatched_tasks if call[0] == "deliver_notification"
    ]
    assert len(dispatched) == 1

    row = (await _queued(db, ws, NotificationType.WORKSPACE_INVITATION))[0]
    assert dispatched[0][1] == (str(row.id),)


# --- Following the link in the email (added after M6 was first marked done) ------
#
# The first M6 suite asserted the token was *in* the invitation email and stopped. The
# link pointed at a page that did not exist, with the token in a query string that
# Architecture §4.6 forbids. These tests start from the rendered email a person would
# receive, take the token out of the link in it, and carry it all the way to membership
# — never using the token the API returned, because the email is the only copy the
# invitee ever has.

LINK = re.compile(r"https?://\S+/invitations\S*")


async def _emailed_link(client, db, ws: str, invitee: str, role=WorkspaceRole.EDITOR):
    """Invite someone, then return (url, token) exactly as the email presents them."""
    created = await client.post(
        f"{WS}/{ws}/invitations", json={"email": invitee, "role": role.value}
    )
    assert created.status_code == 201, created.text

    row = next(
        r
        for r in await _queued(db, ws, NotificationType.WORKSPACE_INVITATION)
        if r.email == invitee
    )
    rendered = notifications.render(row)

    found = LINK.findall(rendered.text)
    assert len(found) == 1, f"expected one invitation link in the email, found {found}"
    url = found[0]
    # The same link must be what the HTML version shows, or the two parts of one email
    # would disagree about where to go — and it must be a real anchor there, not bare
    # text a mail client may or may not choose to make clickable.
    assert f'href="{url}"' in rendered.html

    fragment = urlsplit(url).fragment
    token = unquote(parse_qs(fragment)["token"][0])
    # The emailed token is the live one, not a stale or placeholder value.
    assert token == created.json()["token"]
    return url, token


async def test_the_emailed_link_carries_its_token_only_in_the_fragment(make_client, db):
    """Architecture §4.6: an invitation token never travels in a URL a server can log.

    A fragment is the one part of a URL no server receives. The path and query — both of
    which a frontend server and a reverse proxy write to their access logs — must not
    contain it.
    """
    client, ws, _ = await _workspace(make_client, "m6f-frag@example.com")
    url, token = await _emailed_link(client, db, ws, "m6f-frag-invitee@example.com")

    parts = urlsplit(url)
    assert parts.path == "/invitations"
    assert parts.query == "", "the token (or anything) is in the query string"
    assert token not in parts.path
    assert token not in parts.query
    assert parts.fragment.startswith("token=")
    # It links into the configured frontend, not the API.
    assert url.startswith(settings.frontend_origin.rstrip("/") + "/invitations")


async def test_a_new_user_follows_the_emailed_link_and_joins(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m6f-owner1@example.com")
    _, token = await _emailed_link(owner, db, ws, "m6f-newcomer@example.com")

    visitor = await make_client()
    preview = await visitor.post("/api/v1/invitations/preview", json={"token": token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["workspace_name"] == "Notified"
    assert preview.json()["email"] == "m6f-newcomer@example.com"
    assert preview.json()["requires_signup"] is True

    # The body's email is deliberately different: the server takes the address from the
    # invitation, which is why the sign-up form shows it locked (Design.md §5).
    joined = await visitor.post(
        "/api/v1/auth/signup",
        json={
            "email": "typed-something-else@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "Newcomer",
            "invitation_token": token,
        },
    )
    assert joined.status_code == 201, joined.text
    session = joined.json()
    assert session["user"]["email"] == "m6f-newcomer@example.com"

    membership = {w["id"]: w["role"] for w in session["workspaces"]}
    assert membership.get(ws) == WorkspaceRole.EDITOR.value

    # And the invitation is spent: the same emailed link cannot be used twice.
    again = await (await make_client()).post(
        "/api/v1/invitations/preview", json={"token": token}
    )
    assert again.status_code == 404


async def test_an_existing_user_follows_the_emailed_link_and_accepts(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m6f-owner2@example.com")
    invitee, _ = await new_user(make_client, "m6f-existing@example.com")
    _, token = await _emailed_link(owner, db, ws, "m6f-existing@example.com")

    preview = await invitee.post("/api/v1/invitations/preview", json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["requires_signup"] is False

    accepted = await invitee.post("/api/v1/invitations/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text

    workspaces = (await invitee.get("/api/v1/workspaces")).json()
    assert any(w["id"] == ws and w["role"] == "editor" for w in workspaces)


async def test_the_emailed_link_is_useless_to_a_different_signed_in_user(make_client, db):
    """An invitation is a grant to one address, not a bearer token for whoever holds
    the email — a forwarded invitation must not let someone else join."""
    owner, ws, _ = await _workspace(make_client, "m6f-owner3@example.com")
    _, token = await _emailed_link(owner, db, ws, "m6f-intended@example.com")

    forwarded_to, _ = await new_user(make_client, "m6f-forwarded@example.com")
    refused = await forwarded_to.post("/api/v1/invitations/accept", json={"token": token})
    assert refused.status_code == 403

    workspaces = (await forwarded_to.get("/api/v1/workspaces")).json()
    assert all(w["id"] != ws for w in workspaces)


async def test_a_revoked_invitations_emailed_link_stops_working(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m6f-owner4@example.com")
    _, token = await _emailed_link(owner, db, ws, "m6f-revoked@example.com")

    listed = (await owner.get(f"{WS}/{ws}/invitations")).json()
    invitation_id = next(i["id"] for i in listed if i["email"] == "m6f-revoked@example.com")
    revoked = await owner.delete(f"{WS}/{ws}/invitations/{invitation_id}")
    assert revoked.status_code in (200, 204)

    preview = await (await make_client()).post(
        "/api/v1/invitations/preview", json={"token": token}
    )
    assert preview.status_code == 404
