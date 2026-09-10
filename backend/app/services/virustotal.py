"""VirusTotal API v3 client (Architecture.md §3).

Response shapes here were established by probing the live API on 10 September 2026, not
taken from documentation examples — the same discipline that caught Supabase's
`ListParts` omitting `Size`. What the probe established:

- **Hash hit:** `200`, with engine counts at `data.attributes.last_analysis_stats`
  (`malicious`, `suspicious`, `undetected`, `harmless`, `timeout`, `confirmed-timeout`,
  `failure`, `type-unsupported`).
- **Hash miss:** `404` with `error.code == "NotFoundError"`. A clean, unambiguous signal,
  unlike Supabase returning `400` for a missing bucket.
- **Bad key:** `401` with `error.code == "WrongCredentialsError"`, so a configuration
  fault can never be mistaken for "this file is unknown".
- **No quota headers whatsoever.** Nothing in the response says how much of the daily
  allowance is left, which is why the client-side counter in `vt_budget` is not
  belt-and-braces but the only way to know.
- **`reputation` is not a verdict.** The EICAR test file — malware by construction, 65
  engines flagging it — carries `reputation: 3794`. Reading that field as a safety score
  would invert every answer. Only `last_analysis_stats` is used.

A note on cost, which is why §3b defaults submission off on more than privacy grounds:
a hash lookup is **one** request, while submitting an unknown file is one upload plus up
to `virustotal_analysis_poll_attempts` polls. Against 500 requests a day, submission mode
is an order of magnitude more expensive per file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.models.enums import ScanStatus

logger = logging.getLogger(__name__)


class VirusTotalError(Exception):
    """Base class. Subclasses separate faults that should be retried from those that
    should not, because retrying a wrong API key forever is not resilience."""


class TransientError(VirusTotalError):
    """Timeout, connection failure, 429 or 5xx. Worth retrying."""


class ConfigurationError(VirusTotalError):
    """Missing or rejected credentials. Retrying cannot fix this."""


class NotConfigured(ConfigurationError):
    """No API key present at all."""


@dataclass(frozen=True)
class Verdict:
    """A settled answer about one file's content."""

    status: ScanStatus
    detail: dict[str, Any]

    @property
    def is_flagged(self) -> bool:
        return self.status is ScanStatus.FLAGGED


def _api_key() -> str:
    key = settings.virustotal_api_key
    if key is None or not key.get_secret_value():
        raise NotConfigured(
            "FILESHARE_VIRUSTOTAL_API_KEY is not set, so scanning cannot run."
        )
    return key.get_secret_value()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.virustotal_api_base_url,
        # Sent as a header, never in a URL or query string: URLs end up in access logs
        # and error messages, and the redaction filter should not be the only thing
        # standing between a credential and a log file (Rules.md §3).
        headers={"x-apikey": _api_key()},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


def interpret_stats(stats: dict[str, Any]) -> ScanStatus:
    """Turn engine counts into a verdict.

    `malicious` is the only field that decides. `suspicious` is recorded but does not
    flag on its own: it is the engines' own hedge, and treating a hedge as a conviction
    would block far more files than it protects.

    The threshold is configurable and defaults to 1 — a single engine calling a file
    malicious is enough to withhold it. That will occasionally be a false positive, and
    the consequence is real: a flagged file is unreachable even by its owner. The answer
    to that is the admin override (Architecture.md §3d), which leaves an accountable
    record, rather than a laxer default that quietly serves malware because only one
    scanner noticed.
    """
    malicious = int(stats.get("malicious") or 0)
    return (
        ScanStatus.FLAGGED
        if malicious >= settings.virustotal_malicious_threshold
        else ScanStatus.CLEAN
    )


def _summarise(attributes: dict[str, Any], *, analysis_id: str | None = None) -> dict[str, Any]:
    """What gets stored in `scan_detail` — a summary, never the whole payload.

    The full VirusTotal response is tens of kilobytes of per-engine output. Keeping it
    per version would bloat the table for information nobody reads; keeping the counts
    answers "why was this flagged" and stays diffable.
    """
    stats = attributes.get("last_analysis_stats") or {}
    detail: dict[str, Any] = {
        "source": "virustotal",
        "stats": {
            "malicious": int(stats.get("malicious") or 0),
            "suspicious": int(stats.get("suspicious") or 0),
            "undetected": int(stats.get("undetected") or 0),
            "harmless": int(stats.get("harmless") or 0),
            "failure": int(stats.get("failure") or 0),
            "type_unsupported": int(stats.get("type-unsupported") or 0),
        },
        "threshold": settings.virustotal_malicious_threshold,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }
    if analysis_id:
        detail["analysis_id"] = analysis_id
    last_analysed = attributes.get("last_analysis_date")
    if isinstance(last_analysed, int):
        detail["engines_last_ran_at"] = datetime.fromtimestamp(last_analysed, UTC).isoformat()
    return detail


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise ConfigurationError(
            "VirusTotal rejected the API key. Scanning is misconfigured, not overloaded."
        )
    if response.status_code == 429:
        # Our own budget should make this unreachable; if it happens the assumed limits
        # are wrong, so say so loudly rather than absorbing it.
        raise TransientError(
            "VirusTotal returned 429 despite a reservation - the configured "
            "per-minute/per-day limits do not match the account's real allowance."
        )
    if response.status_code >= 500:
        raise TransientError(f"VirusTotal returned {response.status_code}.")
    if response.status_code >= 400:
        raise VirusTotalError(
            f"Unexpected VirusTotal response {response.status_code}: "
            f"{response.text[:200]}"
        )


async def lookup_hash(sha256: str) -> Verdict | None:
    """One request, no file content transmitted (Architecture.md §3 step 4).

    Returns None when VirusTotal has never seen this content — the caller decides what
    to do with an unknown file, and by default that is *not* to upload it (§3b).
    """
    try:
        async with _client() as client:
            response = await client.get(f"/files/{sha256}")
    except httpx.HTTPError as exc:
        raise TransientError(
            f"VirusTotal request failed: {type(exc).__name__}"
        ) from exc

    if response.status_code == 404:
        return None
    _raise_for_status(response)

    attributes = (response.json().get("data") or {}).get("attributes") or {}
    stats = attributes.get("last_analysis_stats")
    if not isinstance(stats, dict):
        # A 200 without engine counts is not a verdict. Treating it as "clean" would be
        # the worst possible reading of an ambiguous answer.
        raise VirusTotalError(
            f"VirusTotal returned 200 for {sha256[:12]} with no last_analysis_stats."
        )

    return Verdict(status=interpret_stats(stats), detail=_summarise(attributes))


async def submit_file(path: str, *, filename: str, size_bytes: int) -> str:
    """Upload file content for analysis and return the analysis id (§3 step 5).

    Only reached when `virustotal_submit_unknown_files` is enabled. On the free tier the
    submitted file is **shared with the VirusTotal community**, which is why this is off
    by default and why the caller must have decided deliberately to be here.

    Streams from a path rather than taking bytes: a 50MB file held in worker memory
    alongside two concurrent tasks is avoidable waste.
    """
    if size_bytes > settings.virustotal_max_upload_limit_bytes:
        raise VirusTotalError(
            f"{size_bytes} bytes is above VirusTotal's "
            f"{settings.virustotal_max_upload_limit_bytes} byte submission ceiling."
        )

    try:
        async with _client() as client:
            endpoint = "/files"
            if size_bytes > settings.virustotal_direct_upload_limit_bytes:
                # Above the direct limit VirusTotal hands out a one-shot upload URL.
                # Reachable today: the 50MB per-file ceiling sits above 32MB.
                url_response = await client.get("/files/upload_url")
                _raise_for_status(url_response)
                endpoint = (url_response.json() or {}).get("data")
                if not endpoint:
                    raise VirusTotalError(
                        "VirusTotal returned no upload URL for a large file."
                    )

            with open(path, "rb") as handle:
                response = await client.post(
                    endpoint, files={"file": (filename, handle, "application/octet-stream")}
                )
    except httpx.HTTPError as exc:
        raise TransientError(
            f"VirusTotal upload failed: {type(exc).__name__}"
        ) from exc

    _raise_for_status(response)
    analysis_id = ((response.json().get("data") or {}).get("id")) or None
    if not analysis_id:
        raise VirusTotalError("VirusTotal accepted the upload but returned no analysis id.")
    return analysis_id


async def poll_analysis(analysis_id: str) -> Verdict | None:
    """Ask whether a submitted analysis has finished.

    Returns None while it is still queued or running. Each call spends one request from
    the budget, so the caller reserves before every poll rather than once per file.
    """
    try:
        async with _client() as client:
            response = await client.get(f"/analyses/{analysis_id}")
    except httpx.HTTPError as exc:
        raise TransientError(
            f"VirusTotal poll failed: {type(exc).__name__}"
        ) from exc

    _raise_for_status(response)
    attributes = (response.json().get("data") or {}).get("attributes") or {}
    if attributes.get("status") != "completed":
        return None

    stats = attributes.get("stats")
    if not isinstance(stats, dict):
        raise VirusTotalError(
            f"VirusTotal reported analysis {analysis_id[:12]} complete with no stats."
        )
    # A completed analysis reports counts under `stats`, where a file lookup uses
    # `last_analysis_stats`; the summary helper expects the latter shape.
    return Verdict(
        status=interpret_stats(stats),
        detail=_summarise({"last_analysis_stats": stats}, analysis_id=analysis_id),
    )
