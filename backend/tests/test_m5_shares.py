"""M5 share links (Schema.md §9, Architecture.md §4.2-§4.5).

This is the only path to file content that does not start with a membership check, so
the adversarial cases are the point of this file, not an appendix to it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.content import File, FileVersion, ShareLink
from app.models.enums import AuditAction, ScanStatus, ShareLinkType, WorkspaceRole
from app.models.identity import AuditLog, WorkspaceMember
from app.services import shares
from tests.conftest import new_user, upload_file

WS = "/api/v1/workspaces"
SHARES = "/api/v1/shares"
STRONG_PASSWORD = "a-long-share-password"


async def _workspace(make_client, email: str):
    client, session = await new_user(make_client, email)
    ws = (await client.post(WS, json={"name": "Sharing"})).json()["id"]
    return client, ws, session


async def _shared_file(client, ws, name="report.pdf", **link):
    """Upload a file and share it, returning (file_id, raw_token, body)."""
    file_id = (await upload_file(client, ws, name)).json()["id"]
    body = {"link_type": ShareLinkType.PUBLIC.value, **link}
    created = await client.post(f"{WS}/{ws}/files/{file_id}/shares", json=body)
    assert created.status_code == 201, created.text
    payload = created.json()
    return file_id, payload["token"], payload


# ---------------------------------------------------------------------- creation ----


async def test_the_token_is_returned_once_and_never_stored(make_client, db):
    """Rules.md §3: bearer tokens are persisted hashed, raw value shown once."""
    from app.core import security

    client, ws, _ = await _workspace(make_client, "m5-token@example.com")
    _, raw_token, payload = await _shared_file(client, ws)

    assert len(raw_token) > 40, "a share token must be long enough to be unguessable"

    link = await db.get(ShareLink, uuid.UUID(payload["id"]))
    assert link.token_hash == security.hash_token(raw_token)
    assert raw_token not in link.token_hash

    # Nothing in the owner-facing listing can reproduce it.
    listed = (await client.get(f"{WS}/{ws}/files/{payload['file_id']}/shares")).json()
    assert listed[0]["id"] == payload["id"]
    assert "token" not in listed[0]


async def test_two_links_never_share_a_token(make_client):
    client, ws, _ = await _workspace(make_client, "m5-unique@example.com")
    file_id = (await upload_file(client, ws, "a.txt")).json()["id"]
    tokens = set()
    for _ in range(5):
        created = await client.post(
            f"{WS}/{ws}/files/{file_id}/shares",
            json={"link_type": ShareLinkType.PUBLIC.value},
        )
        tokens.add(created.json()["token"])
    assert len(tokens) == 5


async def test_a_token_is_not_derived_from_the_file_id(make_client):
    """Schema.md §9 states this explicitly, so it is asserted explicitly."""
    client, ws, _ = await _workspace(make_client, "m5-derive@example.com")
    file_id, raw_token, _ = await _shared_file(client, ws)
    assert file_id not in raw_token
    assert file_id.replace("-", "") not in raw_token


@pytest.mark.parametrize(
    ("link_type", "missing"),
    [
        (ShareLinkType.PASSWORD_PROTECTED.value, "password"),
        (ShareLinkType.EXPIRING.value, "expiry"),
        (ShareLinkType.PRIVATE.value, "user"),
    ],
)
async def test_each_type_requires_its_own_field(make_client, link_type, missing):
    client, ws, _ = await _workspace(make_client, f"m5-req-{missing}@example.com")
    file_id = (await upload_file(client, ws, "b.txt")).json()["id"]
    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares", json={"link_type": link_type}
    )
    assert response.status_code == 422
    assert missing in response.json()["detail"].lower()


async def test_modifiers_combine_with_any_type(make_client, db):
    """The v1.1 clarification in Schema §9, asserted rather than assumed.

    A `public` link carrying a password and an expiry must be creatable. Rejecting it
    would restore the single-enum reading that made "password-protected AND expiring"
    impossible to express.
    """
    client, ws, _ = await _workspace(make_client, "m5-combine@example.com")
    file_id, token, payload = await _shared_file(
        client,
        ws,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        max_downloads=3,
        password=STRONG_PASSWORD,
    )
    assert payload["link_type"] == ShareLinkType.PUBLIC.value
    assert payload["has_password"] is True
    assert payload["expires_at"] is not None
    assert payload["max_downloads"] == 3

    # And every modifier is actually enforced on that public link.
    assert (await _preview(client, token)).json()["requires_password"] is True
    refused = await _redeem(client, token, password="wrong-password-entirely")
    assert refused.status_code == 403


async def test_an_expiry_in_the_past_is_refused(make_client):
    client, ws, _ = await _workspace(make_client, "m5-past@example.com")
    file_id = (await upload_file(client, ws, "c.txt")).json()["id"]
    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={
            "link_type": ShareLinkType.EXPIRING.value,
            "expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 422
    assert "already passed" in response.json()["detail"]


async def test_a_zero_use_limit_is_refused(make_client):
    """A link dead on arrival is a mistake to catch, not a state to store."""
    client, ws, _ = await _workspace(make_client, "m5-zero@example.com")
    file_id = (await upload_file(client, ws, "d.txt")).json()["id"]
    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value, "max_downloads": 0},
    )
    assert response.status_code == 422


async def test_a_private_link_for_a_stranger_is_refused(make_client):
    client, ws, _ = await _workspace(make_client, "m5-nostranger@example.com")
    file_id = (await upload_file(client, ws, "e.txt")).json()["id"]
    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={
            "link_type": ShareLinkType.PRIVATE.value,
            "allowed_user_email": "nobody-here@example.com",
        },
    )
    assert response.status_code == 422
    assert "no account" in response.json()["detail"].lower()


# --------------------------------------------------------------------- redeeming ----


async def _preview(client, token):
    return await client.get(f"{SHARES}/{token}")


async def _redeem(client, token, password=None):
    return await client.post(
        f"{SHARES}/{token}/download", json={"password": password} if password else {}
    )


async def test_a_public_link_works_for_a_stranger_with_no_account(make_client):
    owner, ws, _ = await _workspace(make_client, "m5-public@example.com")
    _, token, _ = await _shared_file(owner, ws, name="public.pdf")

    anonymous = await make_client()
    preview = await _preview(anonymous, token)
    assert preview.status_code == 200
    assert preview.json()["requires_password"] is False
    assert preview.json()["file_name"] == "public.pdf"

    redeemed = await _redeem(anonymous, token)
    assert redeemed.status_code == 200
    body = redeemed.json()
    assert body["file_name"] == "public.pdf"
    assert body["url"].startswith("http")
    # Short-lived by design: this is the revocation window (Architecture.md §4.3).
    assert body["expires_in_seconds"] == 60


async def test_an_unknown_token_is_a_flat_404(make_client):
    anonymous = await make_client()
    assert (await _preview(anonymous, "not-a-real-token")).status_code == 404
    assert (await _redeem(anonymous, "not-a-real-token")).status_code == 404


async def test_a_password_link_withholds_the_file_name_until_unlocked(make_client):
    """Sending link and password separately is the point; the name is part of the secret."""
    owner, ws, _ = await _workspace(make_client, "m5-pw@example.com")
    _, token, _ = await _shared_file(
        owner,
        ws,
        name="salary-review.pdf",
        link_type=ShareLinkType.PASSWORD_PROTECTED.value,
        password=STRONG_PASSWORD,
    )

    anonymous = await make_client()
    preview = (await _preview(anonymous, token)).json()
    assert preview["requires_password"] is True
    assert preview["file_name"] is None, "the name leaked before the password"

    assert (await _redeem(anonymous, token)).status_code == 403
    assert (await _redeem(anonymous, token, password="not-it-at-all")).status_code == 403

    unlocked = await _redeem(anonymous, token, password=STRONG_PASSWORD)
    assert unlocked.status_code == 200
    assert unlocked.json()["file_name"] == "salary-review.pdf"


async def test_an_expired_link_is_gone_not_forbidden(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m5-expired@example.com")
    _, token, payload = await _shared_file(
        owner,
        ws,
        link_type=ShareLinkType.EXPIRING.value,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    link = await db.get(ShareLink, uuid.UUID(payload["id"]))
    link.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    anonymous = await make_client()
    response = await _redeem(anonymous, token)
    assert response.status_code == 410
    assert "expired" in response.json()["detail"].lower()


async def test_a_revoked_link_stops_working(make_client):
    owner, ws, _ = await _workspace(make_client, "m5-revoke@example.com")
    _, token, payload = await _shared_file(owner, ws)

    anonymous = await make_client()
    assert (await _redeem(anonymous, token)).status_code == 200

    revoked = await owner.delete(f"{WS}/{ws}/shares/{payload['id']}")
    assert revoked.status_code == 204

    after = await _redeem(anonymous, token)
    assert after.status_code == 410
    assert "revoked" in after.json()["detail"].lower()


async def test_the_use_limit_is_enforced_and_counts_resolutions(make_client):
    """`max_downloads` limits redemptions, which is what the docs promise it limits."""
    owner, ws, _ = await _workspace(make_client, "m5-limit@example.com")
    _, token, _ = await _shared_file(owner, ws, max_downloads=2)

    anonymous = await make_client()
    first = await _redeem(anonymous, token)
    assert first.status_code == 200
    assert first.json()["uses_remaining"] == 1

    second = await _redeem(anonymous, token)
    assert second.status_code == 200
    assert second.json()["uses_remaining"] == 0

    third = await _redeem(anonymous, token)
    assert third.status_code == 410
    assert "limit" in third.json()["detail"].lower()


async def test_a_preview_does_not_consume_a_use(make_client):
    owner, ws, _ = await _workspace(make_client, "m5-preview-free@example.com")
    _, token, _ = await _shared_file(owner, ws, max_downloads=1)

    anonymous = await make_client()
    for _ in range(3):
        assert (await _preview(anonymous, token)).status_code == 200
    # The single use is still available.
    assert (await _redeem(anonymous, token)).status_code == 200


# -------------------------------------------------------- private link, adversarial ----


async def test_a_private_link_refuses_anonymous_and_the_wrong_user(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m5-priv-owner@example.com")
    invited, invited_session = await new_user(make_client, "m5-invited@example.com")
    intruder, _ = await new_user(make_client, "m5-intruder@example.com")

    file_id = (await upload_file(owner, ws, "private.pdf")).json()["id"]
    created = await owner.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={
            "link_type": ShareLinkType.PRIVATE.value,
            "allowed_user_email": "m5-invited@example.com",
        },
    )
    assert created.status_code == 201, created.text
    token = created.json()["token"]

    anonymous = await make_client()
    assert (await _redeem(anonymous, token)).status_code == 401

    # Authenticated, holds the token, and still not the named recipient.
    wrong = await _redeem(intruder, token)
    assert wrong.status_code == 403
    assert "different person" in wrong.json()["detail"].lower()

    right = await _redeem(invited, token)
    assert right.status_code == 200


async def test_a_private_link_works_without_workspace_membership(make_client, db):
    """That is the point of sharing: access to one file, not to the workspace."""
    from sqlalchemy import select

    owner, ws, _ = await _workspace(make_client, "m5-nomember@example.com")
    outsider, session = await new_user(make_client, "m5-outsider-priv@example.com")

    file_id = (await upload_file(owner, ws, "for-you.pdf")).json()["id"]
    token = (
        await owner.post(
            f"{WS}/{ws}/files/{file_id}/shares",
            json={
                "link_type": ShareLinkType.PRIVATE.value,
                "allowed_user_email": "m5-outsider-priv@example.com",
            },
        )
    ).json()["token"]

    members = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == uuid.UUID(ws),
                WorkspaceMember.user_id == uuid.UUID(session["user"]["id"]),
            )
        )
    ).scalars().all()
    assert members == [], "the recipient is deliberately not a member"

    assert (await _redeem(outsider, token)).status_code == 200
    # And the link grants nothing beyond that one file.
    assert (await outsider.get(f"{WS}/{ws}/folders")).status_code == 404


# ------------------------------------------------------------------- the scan gate ----


async def test_a_flagged_file_cannot_be_shared_even_after_an_override(make_client, db):
    """The M5 decision, and the reason a stricter second rule exists at all.

    An override is a workspace owner accepting a risk for their own workspace. It must
    not become a public distribution channel for a file the scanner flagged.
    """
    owner, ws, _ = await _workspace(make_client, "m5-flagged@example.com")
    file_id = (await upload_file(owner, ws, "tool.exe")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    file.scan_status = ScanStatus.FLAGGED
    await db.flush()

    refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert refused.status_code == 409
    assert "scan" in refused.json()["detail"].lower()

    # Release it inside the workspace: downloads work again for members...
    released = await owner.post(
        f"{WS}/{ws}/files/{file_id}/versions/{version.id}/scan-override",
        json={"reason": "Reviewed by hand, accepted for internal use only."},
    )
    assert released.status_code == 200
    assert (await owner.get(f"{WS}/{ws}/files/{file_id}/download")).status_code == 200

    # ...and sharing is still refused. This is the whole decision.
    still_refused = await owner.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert still_refused.status_code == 409
    assert shares.version_is_shareable(version) is False


async def test_a_link_stops_working_if_its_file_becomes_unshareable(make_client, db):
    """A link tracks the file, so a later flag must close the link too."""
    owner, ws, _ = await _workspace(make_client, "m5-becomes@example.com")
    file_id, token, _ = await _shared_file(owner, ws, name="later.pdf")

    anonymous = await make_client()
    assert (await _redeem(anonymous, token)).status_code == 200

    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    await db.flush()

    after = await _redeem(anonymous, token)
    assert after.status_code == 403
    assert "withheld" in after.json()["detail"].lower()


async def test_a_deleted_file_closes_its_links(make_client):
    owner, ws, _ = await _workspace(make_client, "m5-deleted@example.com")
    file_id, token, _ = await _shared_file(owner, ws)
    assert (await owner.delete(f"{WS}/{ws}/files/{file_id}")).status_code == 200

    anonymous = await make_client()
    response = await _redeem(anonymous, token)
    assert response.status_code == 410
    assert "no longer available" in response.json()["detail"].lower()


# ---------------------------------------------------------------- who may share ----


async def test_a_viewer_cannot_create_or_revoke_a_link(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m5-viewer-owner@example.com")
    file_id, _, payload = await _shared_file(owner, ws)

    viewer, session = await new_user(make_client, "m5-viewer@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(session["user"]["id"]),
            role=WorkspaceRole.VIEWER,
        )
    )
    await db.flush()

    created = await viewer.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert created.status_code == 403
    assert (await viewer.delete(f"{WS}/{ws}/shares/{payload['id']}")).status_code == 403
    # A viewer may still see that the file is shared.
    assert (await viewer.get(f"{WS}/{ws}/files/{file_id}/shares")).status_code == 200


async def test_an_editor_may_share(make_client, db):
    """Considered choice: an editor can already download and forward the bytes."""
    owner, ws, _ = await _workspace(make_client, "m5-editor-owner@example.com")
    file_id = (await upload_file(owner, ws, "shared.txt")).json()["id"]

    editor, session = await new_user(make_client, "m5-editor@example.com")
    db.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(ws),
            user_id=uuid.UUID(session["user"]["id"]),
            role=WorkspaceRole.EDITOR,
        )
    )
    await db.flush()

    created = await editor.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert created.status_code == 201


async def test_an_outsider_cannot_touch_another_workspaces_links(make_client, db):
    """Adversarial: substituting a foreign share id must not resolve."""
    victim, victim_ws, _ = await _workspace(make_client, "m5-victim@example.com")
    _, victim_token, victim_payload = await _shared_file(victim, victim_ws, name="theirs.pdf")

    attacker, attacker_ws, _ = await _workspace(make_client, "m5-attacker@example.com")
    attacker_file = (await upload_file(attacker, attacker_ws, "mine.txt")).json()["id"]

    # Their workspace, from outside.
    assert (
        await attacker.get(f"{WS}/{victim_ws}/files/{victim_payload['file_id']}/shares")
    ).status_code == 404
    assert (
        await attacker.delete(f"{WS}/{victim_ws}/shares/{victim_payload['id']}")
    ).status_code == 404

    # Their share id, paired with a workspace the attacker does belong to.
    assert (
        await attacker.delete(f"{WS}/{attacker_ws}/shares/{victim_payload['id']}")
    ).status_code == 404

    # Their file id, from inside the attacker's own workspace.
    assert (
        await attacker.post(
            f"{WS}/{attacker_ws}/files/{victim_payload['file_id']}/shares",
            json={"link_type": ShareLinkType.PUBLIC.value},
        )
    ).status_code == 404

    # The victim's link still works, untouched.
    anonymous = await make_client()
    assert (await _redeem(anonymous, victim_token)).status_code == 200
    assert attacker_file  # the attacker's own file was never the point


# ----------------------------------------------------------------------- auditing ----


async def _audit(db, action):
    from sqlalchemy import select

    rows = await db.execute(select(AuditLog).where(AuditLog.action == action))
    return list(rows.scalars().all())


async def test_a_successful_redemption_is_audited_with_the_ip(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m5-audit-ok@example.com")
    _, token, _ = await _shared_file(owner, ws)

    anonymous = await make_client()
    assert (await _redeem(anonymous, token)).status_code == 200

    entries = await _audit(db, AuditAction.SHARE_ACCESSED)
    assert len(entries) == 1
    entry = entries[0]
    # Anonymous: no actor, which is exactly why ip_address exists (Schema §10 v1.1).
    assert entry.actor_id is None
    assert entry.ip_address is not None
    assert entry.audit_metadata["denial_reason"] is None


@pytest.mark.parametrize(
    ("setup", "expected_reason"),
    [
        ("wrong_password", "password_incorrect"),
        ("revoked", "revoked"),
    ],
)
async def test_failures_are_audited_with_their_reason(
    make_client, db, setup, expected_reason
):
    owner, ws, _ = await _workspace(make_client, f"m5-audit-{setup}@example.com")
    if setup == "wrong_password":
        _, token, _ = await _shared_file(
            owner,
            ws,
            link_type=ShareLinkType.PASSWORD_PROTECTED.value,
            password=STRONG_PASSWORD,
        )
        anonymous = await make_client()
        await _redeem(anonymous, token, password="definitely-not-the-password")
    else:
        _, token, payload = await _shared_file(owner, ws)
        await owner.delete(f"{WS}/{ws}/shares/{payload['id']}")
        anonymous = await make_client()
        await _redeem(anonymous, token)

    entries = await _audit(db, AuditAction.SHARE_ACCESS_DENIED)
    assert len(entries) == 1
    assert entries[0].audit_metadata["denial_reason"] == expected_reason


async def test_an_unknown_token_writes_no_audit_row(make_client, db):
    """The M5 decision: attacker-controlled volume does not get to write to our audit log.

    Every attempt against a link that exists is still recorded. What is refused is an
    unbounded write path for anyone who can guess a URL shape.
    """
    anonymous = await make_client()
    for _ in range(5):
        await _redeem(anonymous, f"bogus-{uuid.uuid4().hex}")

    assert await _audit(db, AuditAction.SHARE_ACCESS_DENIED) == []
    assert await _audit(db, AuditAction.SHARE_ACCESSED) == []


async def test_creating_and_revoking_are_audited(make_client, db):
    owner, ws, _ = await _workspace(make_client, "m5-audit-crud@example.com")
    _, _, payload = await _shared_file(owner, ws)
    await owner.delete(f"{WS}/{ws}/shares/{payload['id']}")

    created = await _audit(db, AuditAction.SHARE_CREATED)
    revoked = await _audit(db, AuditAction.SHARE_REVOKED)
    assert len(created) == 1
    assert len(revoked) == 1
    assert created[0].actor_id is not None
    assert created[0].audit_metadata["link_type"] == ShareLinkType.PUBLIC.value


# ------------------------------------------------------------------ the scan rule ----


@pytest.mark.parametrize(
    ("status", "shareable"),
    [
        (ScanStatus.CLEAN, True),
        (ScanStatus.SCAN_SKIPPED, True),
        (ScanStatus.PENDING, False),
        (ScanStatus.SCANNING, False),
        (ScanStatus.FLAGGED, False),
        (ScanStatus.SCAN_FAILED, False),
    ],
)
def test_the_shareable_rule_row_by_row(status, shareable):
    version = FileVersion(
        id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        version_number=1,
        storage_key="k",
        size_bytes=1,
        uploaded_by=uuid.uuid4(),
        scan_status=status,
    )
    assert shares.version_is_shareable(version) is shareable

    # And an override never changes the answer here, unlike for internal downloads.
    version.scan_override_at = datetime.now(UTC)
    version.scan_override_by = uuid.uuid4()
    version.scan_override_reason = "released internally"
    assert shares.version_is_shareable(version) is shareable


async def test_a_share_url_is_signed_with_the_short_share_ttl(make_client, stub_storage):
    """The 60s TTL is the revocation window, so it must actually reach storage.

    Asserted against what the storage layer was asked for, not against the response
    body alone — the response could report 60 while signing for 300.
    """
    owner, ws, _ = await _workspace(make_client, "m5-ttl@example.com")
    _, token, _ = await _shared_file(owner, ws)

    stub_storage["download_urls"].clear()
    anonymous = await make_client()
    assert (await _redeem(anonymous, token)).status_code == 200

    assert len(stub_storage["download_urls"]) == 1
    assert stub_storage["download_urls"][0]["ttl_seconds"] == 60

    # An authenticated download keeps the longer default, so the two really are
    # separate settings rather than one value renamed.
    file_id = (await upload_file(owner, ws, "internal.txt")).json()["id"]
    stub_storage["download_urls"].clear()
    assert (await owner.get(f"{WS}/{ws}/files/{file_id}/download")).status_code == 200
    assert stub_storage["download_urls"][0]["ttl_seconds"] is None


@pytest.mark.parametrize(
    ("status", "expected_phrase"),
    [
        (ScanStatus.PENDING, "not finished its malware scan"),
        (ScanStatus.SCANNING, "not finished its malware scan"),
        (ScanStatus.FLAGGED, "flagged as malicious"),
        (ScanStatus.SCAN_FAILED, "could not be scanned successfully"),
    ],
)
async def test_the_refusal_names_the_actual_reason(make_client, db, status, expected_phrase):
    """Found by manual validation: the refusal explained overrides to someone whose
    scan had simply not finished. A message about a policy that does not apply is worse
    than a vague one, because the reader acts on it."""
    client, ws, _ = await _workspace(make_client, f"m5-why-{status.value}@example.com")
    file_id = (await upload_file(client, ws, "why.txt")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = status
    await db.flush()

    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert expected_phrase in detail
    if status in (ScanStatus.PENDING, ScanStatus.SCANNING):
        assert "override" not in detail.lower(), "explained an irrelevant policy"


async def test_an_overridden_file_gets_the_override_explanation(make_client, db):
    """The one case where mentioning the override is the correct thing to say."""
    client, ws, _ = await _workspace(make_client, "m5-why-override@example.com")
    file_id = (await upload_file(client, ws, "released.exe")).json()["id"]
    file = await db.get(File, uuid.UUID(file_id))
    version = await db.get(FileVersion, file.current_version_id)
    version.scan_status = ScanStatus.FLAGGED
    await db.flush()
    await client.post(
        f"{WS}/{ws}/files/{file_id}/versions/{version.id}/scan-override",
        json={"reason": "Reviewed and accepted for internal use."},
    )

    response = await client.post(
        f"{WS}/{ws}/files/{file_id}/shares",
        json={"link_type": ShareLinkType.PUBLIC.value},
    )
    assert response.status_code == 409
    assert "does not extend to share links" in response.json()["detail"]
