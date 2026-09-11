"""Celery application.

Broker and result backend live on this project's own Redis container (Architecture.md §7),
on separate logical DBs so broker traffic and results do not share a keyspace.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "fileshare",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.maintenance_tasks",
        "app.tasks.content_tasks",
        "app.tasks.scan_tasks",
        "app.tasks.notification_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # A scan task that dies mid-flight must be retried, not silently lost.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    result_expires=60 * 60 * 24,
    broker_connection_retry_on_startup=True,
)

# Scheduled maintenance (Architecture.md §2 and §6).
celery_app.conf.beat_schedule = {
    "reconcile-storage-usage": {
        "task": "app.tasks.maintenance_tasks.reconcile_storage_usage",
        "schedule": crontab(hour="3", minute="0"),
    },
    "sweep-abandoned-multipart-uploads": {
        "task": "app.tasks.maintenance_tasks.sweep_abandoned_multipart_uploads",
        "schedule": crontab(hour="*/6", minute="15"),
    },
    # Architecture.md §3a: a worker killed mid-scan leaves a version in `scanning` with
    # nothing scheduled, and a version left `pending` by a misconfigured key has nothing
    # to restart it either. Both are recovered here rather than waiting for someone to
    # notice a file that never became downloadable.
    "redrive-stalled-scans": {
        "task": "app.tasks.scan_tasks.redrive_stalled_scans",
        "schedule": crontab(minute="*/15"),
    },
    # Architecture.md §5b: a notification row is dispatched when it is queued, so
    # this sweep is the recovery path — rows committed but never dispatched because the
    # process died in between, and retries lost with a worker. Same reasoning as
    # redrive-stalled-scans, and bounded so an outage does not become a send burst.
    "sweep-pending-notifications": {
        "task": "app.tasks.notification_tasks.sweep_pending_notifications",
        "schedule": crontab(minute="*/10"),
    },
    # Architecture.md §6b: delete the bytes of anything deleted more than the retention
    # period ago. Described since M2 and never scheduled until M7.
    "purge-expired-deletions": {
        "task": "app.tasks.maintenance_tasks.purge_expired_deletions",
        "schedule": crontab(hour="4", minute="10"),
    },
    # Supabase free-tier projects pause after 7 days of inactivity (PRD §10).
    # Daily is ample headroom; removed when the project returns to AWS S3.
    "storage-keepalive": {
        "task": "app.tasks.maintenance_tasks.storage_keepalive",
        "schedule": crontab(hour="6", minute="30"),
    },
}
