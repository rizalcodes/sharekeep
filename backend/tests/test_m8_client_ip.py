"""Which address is the client (Architecture §4.4, M8).

Found before M8 by running the attack rather than reading the code: the client address
came from the LEFTMOST X-Forwarded-For entry, which is whatever the client sends. One
claimed address was refused at attempt 31 as designed; a new claimed address per request
made 80 share-token attempts with no refusal. These tests run that attack again.
"""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.core.config import settings
from app.models.enums import AuditAction
from app.models.identity import AuditLog
from app.services import audit

PEER = "172.18.0.5"  # what the TCP peer looks like behind a proxy on a Docker network


def _request(forwarded: str | None, peer: str = PEER) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 40000),
            "query_string": b"",
        }
    )


@pytest.fixture
def hops(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setattr(settings, "trusted_proxy_hops", value)

    return _set


def test_no_trusted_proxy_ignores_the_header_entirely(hops):
    hops(0)
    assert audit.client_ip(_request("203.0.113.9")) == PEER


def test_one_proxy_means_the_rightmost_entry(hops):
    hops(1)
    # The client wrote the first entry; the proxy appended the second.
    assert audit.client_ip(_request("203.0.113.9, 198.51.100.4")) == "198.51.100.4"


def test_two_proxies_means_second_from_the_right(hops):
    hops(2)
    forged, client, outer_proxy = "203.0.113.9", "198.51.100.4", "162.158.1.1"
    assert audit.client_ip(_request(f"{forged}, {client}, {outer_proxy}")) == client


def test_fewer_entries_than_hops_is_not_guessed_at(hops):
    """Changed at M8c. This used to return the leftmost entry on the theory that every
    entry present came from a trusted proxy. In production that theory produced a wrong
    but plausible answer — Cloudflare's own edge address, recorded as the visitor on every
    audited action — because the proxy was replacing the header rather than appending to
    it. An obviously internal address makes the misconfiguration visible instead."""
    hops(2)
    assert audit.client_ip(_request("198.51.100.4")) == PEER


def test_no_header_behind_a_proxy_falls_back_to_the_peer(hops):
    hops(1)
    assert audit.client_ip(_request(None)) == PEER
    assert audit.client_ip(_request("  ,  ")) == PEER


def test_an_entry_that_is_not_an_address_is_not_believed(hops):
    hops(1)
    assert audit.client_ip(_request("198.51.100.4, not-an-ip")) == PEER
    assert audit.client_ip(_request("'; DROP TABLE audit_log; --")) == PEER


def test_ipv6_is_accepted_and_normalised(hops):
    hops(1)
    assert audit.client_ip(_request("2001:DB8:0:0:0:0:0:1")) == "2001:db8::1"


async def _preview_attempts(client, headers_for) -> list[int]:
    statuses = []
    for attempt in range(40):
        response = await client.get(
            f"/api/v1/shares/{secrets.token_urlsafe(48)}", headers=headers_for(attempt)
        )
        statuses.append(response.status_code)
    return statuses


async def test_rotating_a_forged_address_no_longer_escapes_the_share_limit(client, hops):
    """The attack from the M8 probe, behind one trusted proxy.

    The client varies the part it controls; the proxy appends the same real address
    every time. Before the fix this never reached 429.
    """
    hops(1)
    statuses = await _preview_attempts(
        client, lambda n: {"X-Forwarded-For": f"203.0.113.{n}, 198.51.100.4"}
    )
    assert 429 in statuses, statuses
    assert statuses.index(429) == settings.share_attempts_per_ip_per_minute


async def test_without_a_proxy_the_header_cannot_buy_fresh_budget_either(client, hops):
    hops(0)
    statuses = await _preview_attempts(
        client, lambda n: {"X-Forwarded-For": f"203.0.113.{n}"}
    )
    assert statuses.index(429) == settings.share_attempts_per_ip_per_minute


async def test_the_audit_log_records_the_proxy_observed_address(client, db, hops):
    """A visitor could previously choose the IP written into the security record."""
    hops(1)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody-m8@example.com", "password": "wrong-password-123"},
        headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.77"},
    )
    assert response.status_code == 401

    row = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == AuditAction.LOGIN_FAILED)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert str(row.ip_address) == "198.51.100.77"


async def test_a_malformed_header_cannot_break_the_audit_write(client, hops):
    hops(1)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody-m8@example.com", "password": "wrong-password-123"},
        headers={"X-Forwarded-For": "definitely not an address"},
    )
    assert response.status_code == 401


def test_a_shorter_chain_than_configured_is_reported_not_guessed(hops, caplog):
    """M8c: Caddy replaced X-Forwarded-For instead of appending, so the backend saw one
    entry — Cloudflare's edge — and the old fallback recorded that as the visitor. Wrong
    but plausible, on every audited action, with nothing appearing broken."""
    hops(2)
    with caplog.at_level("WARNING"):
        assert audit.client_ip(_request("172.71.81.120")) == PEER
    assert "trusted" in caplog.text.lower()
