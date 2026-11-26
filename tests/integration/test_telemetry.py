from datetime import timedelta

import pytest

from app.api.v1.endpoints import telemetry
from app.core import clock
from app.core.database import SessionLocal
from app.core.rate_limit import SlidingWindowLimiter
from app.infrastructure.db.models import CrashReport, TelemetryEvent
from app.workers import reminder_jobs
from scripts.telemetry_report import report

V1 = "/api/v1"
INSTALL = {"install_id": "inst-12345678", "app_version": "1.0.0", "platform": "android"}
@pytest.fixture(autouse=True)
def _fresh_limits():
    for lim in telemetry.LIMITS.values():
        lim.reset()


STACK = "#0      TaskPage.build (package:foyo/features/tasks/task_page.dart:42:7)\n#1      main (package:foyo/main.dart:9:3)"


def test_events_without_account_and_limits(client):
    now = clock.now().isoformat()
    ok = {**INSTALL, "events": [{"name": "screen_view", "at": now, "props": {"screen": "/today"}}]}
    assert client.post(f"{V1}/telemetry/events", json=ok).status_code == 204
    assert client.post(f"{V1}/telemetry/events", json={**ok, "install_id": "x"}).status_code == 422
    bad_name = {**INSTALL, "events": [{"name": "Has Spaces", "at": now}]}
    assert client.post(f"{V1}/telemetry/events", json=bad_name).status_code == 422
    too_many = {**INSTALL, "events": [{"name": "x", "at": now}] * 101}
    assert client.post(f"{V1}/telemetry/events", json=too_many).status_code == 422
    nested = {**INSTALL, "events": [{"name": "x", "at": now, "props": {"a": {"b": 1}}}]}
    assert client.post(f"{V1}/telemetry/events", json=nested).status_code == 422


def test_same_crash_is_grouped_and_old_data_purged(client):
    for line in ("42:7", "43:9"):  # same crash, the line moved between versions
        body = {**INSTALL, "error": "Null check operator used on a null value", "stack": STACK.replace("42:7", line)}
        assert client.post(f"{V1}/telemetry/crashes", json=body).status_code == 204
    with SessionLocal() as db:
        assert len({c.fingerprint for c in db.query(CrashReport)}) == 1
    text = report(7)
    assert "2× sur 1 tél." in text and "Null check" in text

    with SessionLocal() as db:
        assert reminder_jobs.purge_telemetry(db, clock.now() + timedelta(days=91)) >= 2
        assert db.query(CrashReport).count() == 0 and db.query(TelemetryEvent).count() == 0


def test_rate_limits(client, monkeypatch):
    now = clock.now().isoformat()
    monkeypatch.setitem(telemetry.LIMITS, "events_install", SlidingWindowLimiter(2, 60))
    monkeypatch.setitem(telemetry.LIMITS, "events_volume", SlidingWindowLimiter(150, 3600))
    batch = {**INSTALL, "events": [{"name": "screen_view", "at": now}] * 100}
    assert client.post(f"{V1}/telemetry/events", json=batch).status_code == 204
    r = client.post(f"{V1}/telemetry/events", json=batch)  # 200 events > 150 per hour for this install
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    small = {**INSTALL, "events": [{"name": "screen_view", "at": now}]}
    r = client.post(f"{V1}/telemetry/events", json=small)  # 3rd request this minute for this install
    assert r.status_code == 429 and r.json()["detail"].startswith("Trop d’envois")
    other = {**small, "install_id": "inst-87654321"}
    assert client.post(f"{V1}/telemetry/events", json=other).status_code == 204  # another phone is fine

    # Per IP, counted before the body is validated: a flood of bad requests is stopped too.
    monkeypatch.setitem(telemetry.LIMITS, "crashes_ip", SlidingWindowLimiter(3, 60))
    assert [client.post(f"{V1}/telemetry/crashes", json={"bad": 1}).status_code for _ in range(4)] == [422, 422, 422, 429]
    good = {**INSTALL, "error": "x", "stack": ""}
    assert client.post(f"{V1}/telemetry/crashes", json=good).status_code == 429
