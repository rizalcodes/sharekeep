"""Resend API client (Architecture.md §5b-§5c).

Kept separate from `notifications.py` for the same reason `virustotal.py` is separate
from `scan.py`: the client is the part that gets probed, stubbed and eventually
replaced, and the domain logic should not have to change when it is.

Everything below was established by probing the live API on 11 September 2026 with the
project's own key, not read from documentation. What the probe established:

- **The key in use is restricted to sending.** `GET /emails/{id}`, `/domains` and
  `/api-keys` all return `401 restricted_api_key`. So **delivery is unobservable to
  us** — we cannot know whether a message arrived, bounced or was opened. This is why
  `notifications.status = sent` means "accepted by the provider" and nothing more, and
  why `provider_message_id` is stored for a human to follow up with.
- **An unverified sending domain is refused 403** (`validation_error`, naming the
  domain). `noreply@example.com` — which this project's configuration shipped with —
  would have failed every single send. `onboarding@resend.dev` is accepted.
- **Without a verified domain the only permitted recipient is the account owner**, also
  403, and the error text names that address. A notification to anyone else is
  permanently undeliverable until a domain is verified, which is what
  `notification_redirect_to` exists for.
- **`delivered@resend.dev` and `bounced@resend.dev` are exempt** from that rule and
  accepted. They are the only way the delivery and failure paths can be exercised
  without a verified domain.
- **`example.com` recipients get 422, not 403** — a different code with a different
  message, so recipient-shape rejections and the owner-only rule are distinguishable.
- **Six sends with no pause drew no 429**, despite a documented 2/second ceiling. The
  limiter is therefore built to the documented figure: an unobserved limit is not an
  absent one, and discovering otherwise in production would cost real notifications.
- **Cloudflare sits in front of the API and rejects some clients by user agent** with a
  bare `error code: 1010` and no JSON at all. That is not a Resend response, so the
  client sends an explicit User-Agent and treats an unparseable body as transient
  rather than reading it as a verdict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Resend's own simulator addresses. Exempt from the owner-only sandbox rule, which makes
# them the only usable fixtures for an end-to-end delivery check.
SIMULATOR_DELIVERED = "delivered@resend.dev"
SIMULATOR_BOUNCED = "bounced@resend.dev"


class ResendError(Exception):
    """Base class. Subclasses separate what is worth retrying from what is not, because
    retrying a refusal that is a property of the configuration only burns budget."""


class TransientError(ResendError):
    """Timeout, connection failure, 429, 5xx, or a response we cannot parse. Retry."""


class PermanentError(ResendError):
    """The provider refused this specific message and will refuse it again.

    An unverified sending domain, a recipient the sandbox does not permit, a malformed
    address. Retrying cannot change any of these — only a configuration change can.
    """


class NotConfigured(ResendError):
    """No API key at all.

    Deliberately neither transient nor permanent: nothing is wrong with the
    notification, so the row stays `pending` rather than being marked failed. The same
    distinction `virustotal.py` draws.
    """


@dataclass(frozen=True)
class Accepted:
    """What we are actually allowed to know about a send."""

    provider_message_id: str


def _api_key() -> str:
    key = settings.resend_api_key
    if key is None or not key.get_secret_value():
        raise NotConfigured(
            "FILESHARE_RESEND_API_KEY is not set, so notifications cannot be sent."
        )
    return key.get_secret_value()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.resend_api_base_url,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            # Required in practice, not by the API: Cloudflare fronts api.resend.com
            # and refuses some default client signatures with a bare "error code: 1010"
            # and no JSON body. Found by probe, when urllib's default agent was blocked
            # while curl's was not.
            "User-Agent": "fileshare-backend/1.0",
        },
        timeout=httpx.Timeout(30.0, connect=10.0),
    )


def _describe(response: httpx.Response) -> str:
    """The provider's own wording, so `last_error` is quotable rather than paraphrased."""
    try:
        body: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:200]}"
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        name = body.get("name")
        if message:
            return f"HTTP {response.status_code} {name or ''}: {message}".strip()
    return f"HTTP {response.status_code}: {response.text[:200]}"


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    detail = _describe(response)

    if status == 401:
        # A restricted key is expected and fine for sending; a rejected one is not.
        raise PermanentError(f"Resend rejected the API key. {detail}")
    if status == 403:
        # Both sandbox refusals land here: unverified sending domain, and a recipient
        # other than the account owner. Neither improves by being retried.
        raise PermanentError(detail)
    if status == 422:
        raise PermanentError(detail)
    if status == 429:
        raise TransientError(f"Resend rate-limited the request. {detail}")
    if status >= 500:
        raise TransientError(f"Resend is unavailable. {detail}")
    if status >= 400:
        # An unclassified 4xx is treated as permanent: the request is wrong, and
        # repeating an identically wrong request is not a recovery strategy.
        raise PermanentError(detail)


async def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    sender: str | None = None,
) -> Accepted:
    """Hand one message to Resend.

    Returns only the provider's message id, because that is the entirety of what the
    restricted key lets us learn. A successful return means **accepted**, never
    delivered.
    """
    payload: dict[str, Any] = {
        "from": sender or settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if html:
        payload["html"] = html

    try:
        async with _client() as client:
            response = await client.post("/emails", json=payload)
    except httpx.HTTPError as exc:
        raise TransientError(f"Could not reach Resend: {exc}") from exc

    _raise_for_status(response)

    try:
        body = response.json()
    except ValueError as exc:
        # A 200 we cannot parse is not a send we can claim. Treated as transient so the
        # row is retried rather than recorded as sent on no evidence.
        raise TransientError(
            f"Resend returned HTTP {response.status_code} with an unreadable body."
        ) from exc

    message_id = body.get("id") if isinstance(body, dict) else None
    if not message_id:
        raise TransientError("Resend accepted the message but returned no id.")

    # Deliberately not logged: subject lines and recipients are the content of a
    # notification, and Rules.md §3 keeps notification bodies out of the log.
    logger.info("resend accepted a message (id=%s)", message_id)
    return Accepted(provider_message_id=str(message_id))
