"""Notification delivery (Architecture.md §5b).

Queueing is transactional and lives in `services/notifications.py`; this module does the
part that cannot be — handing a message to an external provider and recording what
happened.

Three properties are load-bearing here, and each exists because of a specific way this
could go wrong:

- **Delivery is idempotent at the row.** A task that succeeds at the provider and then
  dies before committing would otherwise send again on retry. `sent_at` is the guard,
  claimed under a row lock, because an email that has gone out cannot be recalled — a
  duplicate is not a retry, it is a second message to a real person.
- **Failures are separated by kind.** A transient failure is retried; a permanent refusal
  is recorded and never retried, since repeating a request the provider refuses on
  principle only burns free-tier budget; a missing key leaves the row `pending`, because
  nothing is wrong with the notification.
- **The sweep is the recovery path, not the primary one.** A row is dispatched directly
  when it is queued. The sweep exists for rows whose dispatch never happened (the
  process died between commit and dispatch) or whose retries were lost with a worker —
  the same reasoning as `redrive_stalled_scans`.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from app.core import async_bridge
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.enums import NotificationStatus
from app.models.identity import Notification
from app.services import notifications, resend

logger = logging.getLogger(__name__)


def _run(coro):
    """One loop per worker process, not one per task (app.core.async_bridge)."""
    return async_bridge.run(coro)


async def _deliver(notification_id: uuid.UUID) -> str:
    async with SessionLocal() as db:
        # The lock and the `sent_at` check are one step: two workers racing on the same
        # row must not both decide it is unsent.
        result = await db.execute(
            select(Notification)
            .where(Notification.id == notification_id)
            .with_for_update()
        )
        notification = result.scalar_one_or_none()

        if notification is None:
            return "gone"
        if notification.sent_at is not None:
            # Already accepted by the provider. Returning rather than re-sending is the
            # whole point of the guard.
            return "already_sent"
        if notification.status is NotificationStatus.FAILED:
            return "already_failed"

        if not settings.notifications_enabled:
            # Left pending on purpose: the notification is fine, the configuration is
            # not. Marking it failed would discard a message that becomes deliverable
            # the moment a key is set.
            logger.info("no Resend key configured; notification stays pending")
            return "not_configured"

        to, redirected_from = notifications.resolve_recipient(notification)
        rendered = notifications.render(notification, redirected_from=redirected_from)

        notification.attempts += 1
        attempt = notification.attempts

        try:
            accepted = await resend.send_email(
                to=to,
                subject=rendered.subject,
                text=rendered.text,
                html=rendered.html,
            )
        except resend.NotConfigured:
            # Reachable if the key is removed between the check above and the send.
            notification.attempts -= 1
            await db.commit()
            return "not_configured"
        except resend.PermanentError as exc:
            notification.status = NotificationStatus.FAILED
            notification.last_error = str(exc)[:1024]
            await db.commit()
            logger.warning("notification refused permanently: %s", exc)
            return "failed_permanent"
        except resend.TransientError as exc:
            notification.last_error = str(exc)[:1024]
            exhausted = attempt >= settings.notification_max_attempts
            if exhausted:
                # Out of attempts. Recorded as failed with the last reason rather than
                # left pending forever, so the sweep does not retry it until the end of
                # time and a reader can see why it stopped.
                notification.status = NotificationStatus.FAILED
            await db.commit()
            if exhausted:
                return "failed_exhausted"
            raise

        notification.status = NotificationStatus.SENT
        notification.sent_at = notifications.utcnow()
        notification.provider_message_id = accepted.provider_message_id
        notification.redirected_to = to if redirected_from else None
        notification.last_error = None
        await db.commit()
        return "sent"


@celery_app.task(
    name="app.tasks.notification_tasks.deliver_notification",
    bind=True,
    max_retries=None,
    autoretry_for=(),
)
def deliver_notification(self, notification_id: str) -> dict[str, Any]:
    """Deliver one queued notification.

    `max_retries=None` with an explicit ceiling on the row rather than Celery's counter:
    the row is the durable record, so a worker restart does not reset the count, and the
    sweep can tell an exhausted row from one still in flight.
    """
    target = uuid.UUID(notification_id)
    try:
        outcome = _run(_deliver(target))
    except resend.TransientError as exc:
        # Backoff doubles per attempt, capped. The provider's documented ceiling is 2
        # requests/second, so a burst of failures should not become a burst of retries.
        countdown = min(60 * 2**self.request.retries, 3600)
        logger.info("notification delivery retrying in %ss: %s", countdown, exc)
        raise self.retry(countdown=countdown, exc=exc) from exc
    return {"notification_id": notification_id, "outcome": outcome}


def dispatch(notification_ids: list[uuid.UUID]) -> None:
    """Hand queued rows to the worker.

    Call this **after** the transaction that created them commits — dispatching inside
    it would let a worker pick up a row that a rollback then removed, and the email
    would describe something that never happened.
    """
    for notification_id in notification_ids:
        deliver_notification.delay(str(notification_id))


@celery_app.task(name="app.tasks.notification_tasks.sweep_pending_notifications")
def sweep_pending_notifications() -> dict[str, Any]:
    """Re-dispatch pending notifications, oldest first.

    The recovery path for rows that were committed but never dispatched, and for retries
    lost with a worker. Bounded by `notification_batch_size`: an unbounded sweep after an
    outage would hand the provider thousands of messages at once and hit a rate limit
    that this design is otherwise careful to stay under.
    """
    return _run(_sweep())


async def _sweep() -> dict[str, Any]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(Notification.id)
            .where(
                Notification.status == NotificationStatus.PENDING,
                Notification.sent_at.is_(None),
                Notification.attempts < settings.notification_max_attempts,
            )
            .order_by(Notification.created_at)
            .limit(settings.notification_batch_size)
        )
        ids = list(result.scalars().all())

    for notification_id in ids:
        deliver_notification.delay(str(notification_id))
    if ids:
        logger.info("swept %d pending notification(s)", len(ids))
    return {"dispatched": len(ids)}
