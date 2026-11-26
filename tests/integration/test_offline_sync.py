"""Actions made offline and replayed later (spec §7: offline use, first timestamped validation wins)."""

from datetime import datetime, timedelta

from app.core import clock

V1 = "/api/v1"


def test_same_idempotency_key_is_applied_once(client, awa):
    before = client.get(f"{V1}/finances/summary", headers=awa).json()["expense"]
    body = {"type": "expense", "amount": 1200, "category_id": "transport", "account_id": "a-cash"}
    h = {**awa, "Idempotency-Key": "op-offline-1"}
    first = client.post(f"{V1}/operations", json=body, headers=h)
    again = client.post(f"{V1}/operations", json=body, headers=h)  # replayed after a dropped connection
    assert first.status_code == again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert again.headers.get("Idempotent-Replay") == "true"
    assert client.get(f"{V1}/finances/summary", headers=awa).json()["expense"] == before + 1200
    other = client.post(f"{V1}/operations", json=body, headers={**awa, "Idempotency-Key": "op-offline-2"})
    assert other.json()["id"] != first.json()["id"]  # a new key is a new action


def test_keys_are_per_account(client, awa, koffi):
    body = {"type": "expense", "amount": 500, "category_id": "transport", "account_id": "a-cash"}
    a = client.post(f"{V1}/operations", json=body, headers={**awa, "Idempotency-Key": "same"})
    k = client.post(f"{V1}/operations", json=body, headers={**koffi, "Idempotency-Key": "same"})
    assert a.json()["id"] != k.json()["id"]


def test_first_timestamped_validation_wins(client, awa, koffi):
    now = clock.now()
    # Koffi ticked "Préparer le dîner" offline at 18:40; Awa (online) at 18:55. Koffi's arrives last.
    awa_done = client.post(
        f"{V1}/tasks/t-diner/complete",
        json={"actual_seconds": 2700, "completed_at": now.replace(hour=18, minute=55).isoformat()},
        headers=awa,
    ).json()
    assert awa_done["points_earned"] == 10
    koffi_done = client.post(
        f"{V1}/tasks/t-diner/complete",
        json={"actual_seconds": 1800, "completed_at": now.replace(hour=18, minute=40).isoformat()},
        headers=koffi,
    ).json()
    assert koffi_done["points_earned"] == 0  # no double points
    assert koffi_done["task"]["actual_minutes"] == 30  # the earlier validation is the one kept
    # A later one doesn't overwrite it.
    late = client.post(
        f"{V1}/tasks/t-diner/complete",
        json={"actual_seconds": 60, "completed_at": now.replace(hour=20, minute=0).isoformat()},
        headers=awa,
    ).json()
    assert late["task"]["actual_minutes"] == 30


def test_ticked_yesterday_offline_lands_on_yesterday(client, awa):
    yesterday = clock.today() - timedelta(days=1)
    r = client.post(
        f"{V1}/tasks/t-bain/complete",
        json={
            "actual_seconds": 900,
            "date": yesterday.isoformat(),
            "completed_at": datetime.combine(yesterday, datetime.min.time()).replace(hour=19, minute=40).isoformat(),
        },
        headers=awa,
    )
    assert r.status_code == 200
    report = client.get(f"{V1}/reports/daily", params={"date": yesterday.isoformat()}, headers=awa).json()
    assert report["date"] == yesterday.isoformat()


def test_utc_times_from_phones_are_converted(client, awa):
    client.post(f"{V1}/tasks/t-machine/stop", headers=awa)  # running on demo days: one task at a time
    r = client.post(f"{V1}/tasks/t-wc/start", json={"started_at": "2026-11-24T09:30:00Z"}, headers=awa)
    assert r.status_code == 200


def test_explicit_intake_state_survives_replays(client, awa):
    url = f"{V1}/health/intakes/i5/toggle"
    assert client.post(url, params={"taken": "true"}, headers=awa).json()["taken"] is True
    assert client.post(url, params={"taken": "true"}, headers=awa).json()["taken"] is True  # replay: still taken
    assert client.post(url, params={"taken": "false"}, headers=awa).json()["taken"] is False
