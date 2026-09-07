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
}
