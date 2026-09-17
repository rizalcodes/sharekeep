"""Readiness no longer depends on a busy worker answering a ping (Architecture §7a, M8)."""

from __future__ import annotations

import time
import uuid

import redis

from app.core import worker_heartbeat
from app.core.config import settings
from app.routers import health


class _FakeRedis:
    """Only what `report` reads. A real Redis would also hold the live worker's own key,
    and a test asserting "no worker" would then depend on whether Docker is running."""

    def __init__(self, values: dict[str, str] | None = None, broken: bool = False):
        self.values = values or {}
        self.broken = broken

    async def scan_iter(self, match: str):
        if self.broken:
            raise redis.ConnectionError("down")
        prefix = match.rstrip("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    async def get(self, key: str):
        return self.values.get(key)


async def test_no_heartbeat_means_no_live_worker():
    report = await worker_heartbeat.report(_FakeRedis())
    assert report["alive"] is False and report["workers"] == 0


async def test_a_recent_heartbeat_is_a_live_worker():
    now = time.time()
    report = await worker_heartbeat.report(
        _FakeRedis(
            {
                f"{worker_heartbeat.KEY_PREFIX}a": str(now - 20),
                f"{worker_heartbeat.KEY_PREFIX}b": str(now - 3),
            }
        )
    )
    assert report["alive"] is True
    assert report["workers"] == 2
    assert 2 <= report["last_seen_seconds"] <= 5


async def test_unreadable_redis_says_it_cannot_tell_rather_than_dead():
    report = await worker_heartbeat.report(_FakeRedis(broken=True))
    assert report["alive"] is None


def test_the_beat_writes_an_expiring_key_and_removes_it_on_stop(monkeypatch):
    """Against the real Redis: the thing that must work is the write and the expiry."""
    prefix = f"fileshare-test-heartbeat-{uuid.uuid4().hex}:"
    monkeypatch.setattr(worker_heartbeat, "KEY_PREFIX", prefix)
    client = redis.Redis.from_url(settings.redis_url)

    worker_heartbeat.start("celery@test")
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not client.exists(f"{prefix}celery@test"):
            time.sleep(0.05)
        assert client.exists(f"{prefix}celery@test")
        ttl = client.ttl(f"{prefix}celery@test")
        assert 0 < ttl <= worker_heartbeat.EXPIRY_SECONDS
    finally:
        worker_heartbeat.stop()
    assert not client.exists(f"{prefix}celery@test")


async def _ready_with(monkeypatch, client, worker_report: dict):
    async def ok():
        return {"ok": True}

    async def report(_client):
        return worker_report

    monkeypatch.setattr(health, "_check_redis", ok)
    monkeypatch.setattr(health, "_check_storage", ok)
    monkeypatch.setattr(worker_heartbeat, "report", report)
    return await client.get("/health/ready")


async def test_a_dead_worker_does_not_make_the_api_unready(client, monkeypatch):
    """The M7c failure, stated as the property that matters: 200, not 503."""
    response = await _ready_with(
        monkeypatch, client, {"alive": False, "workers": 0, "detail": "no worker heartbeat"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert "celery" not in body["checks"]
    # ...and it is still plainly reported, beside the verdict.
    assert body["worker"]["alive"] is False


async def test_a_live_worker_is_reported_beside_the_verdict(client, monkeypatch):
    response = await _ready_with(
        monkeypatch, client, {"alive": True, "workers": 1, "last_seen_seconds": 4.0}
    )
    assert response.status_code == 200
    assert response.json()["worker"] == {"alive": True, "workers": 1, "last_seen_seconds": 4.0}


async def test_storage_failing_still_makes_the_api_unready(client, monkeypatch):
    async def ok():
        return {"ok": True}

    async def down():
        return {"ok": False, "detail": "EndpointConnectionError"}

    async def report(_client):
        return {"alive": True, "workers": 1, "last_seen_seconds": 1.0}

    monkeypatch.setattr(health, "_check_redis", ok)
    monkeypatch.setattr(health, "_check_storage", down)
    monkeypatch.setattr(worker_heartbeat, "report", report)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"

