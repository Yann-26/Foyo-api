"""Spec §9: free version limits, Premium features, 7-day trial, RevenueCat webhook."""

import time

import pytest

from app.core import clock
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.infrastructure.db.models import Bill, Household

V1 = "/api/v1"


@pytest.fixture()
def free(client):
    """A brand-new household on the free version (the demo household is Premium)."""
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Fatou", "email": "fatou@example.test", "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _task(i, kind="daily", **extra):
    return {"name": f"Tâche {i}", "category_id": "menage", "estimated_minutes": 10, "recurrence": {"kind": kind}, **extra}


def _is_402(r, words):
    assert r.status_code == 402, r.text
    assert words in r.json()["detail"] and "Essai gratuit" in r.json()["detail"]


def test_free_limits(client, free):
    # 3 members: Fatou + 2 invitations, then refused.
    for e in ("a@example.test", "b@example.test"):
        assert client.post(f"{V1}/households/current/invitations", json={"email": e}, headers=free).status_code == 201
    _is_402(client.post(f"{V1}/households/current/invitations", json={"email": "c@example.test"}, headers=free), "3 membres")
    # 10 recurring tasks; one-off tasks are not counted.
    for i in range(10):
        assert client.post(f"{V1}/tasks", json=_task(i), headers=free).status_code == 201
    _is_402(client.post(f"{V1}/tasks", json=_task(11), headers=free), "10 tâches récurrentes")
    assert client.post(f"{V1}/tasks", json=_task(12, kind="once"), headers=free).status_code == 201
    # 2 accounts.
    acc = [client.post(f"{V1}/accounts", json={"name": n, "type": "cash"}, headers=free).json()["id"] for n in ("A", "B")]
    _is_402(client.post(f"{V1}/accounts", json={"name": "C"}, headers=free), "2 comptes")
    # 50 operations a month.
    op = {"type": "expense", "amount": 100, "category_id": "transport", "account_id": acc[0]}
    for _ in range(50):
        assert client.post(f"{V1}/operations", json=op, headers=free).status_code == 201
    _is_402(client.post(f"{V1}/operations", json=op, headers=free), "50 opérations par mois")
    # 3 budgets.
    for cat in ("alimentation", "transport", "loisirs"):
        assert client.put(f"{V1}/budgets", json={"category_id": cat, "planned": 1000}, headers=free).status_code == 200
    assert client.put(f"{V1}/budgets", json={"category_id": "loisirs", "planned": 2000}, headers=free).status_code == 200
    _is_402(client.put(f"{V1}/budgets", json={"category_id": "sante", "planned": 1000}, headers=free), "3 budgets")
    usage = client.get(f"{V1}/subscriptions/current", headers=free).json()["usage"]
    assert usage == {"members": 3, "recurring_tasks": 10, "accounts": 2, "operations_per_month": 50, "budgets": 3}


def test_premium_only_features(client, free):
    _is_402(client.post(f"{V1}/tasks", json=_task(1, rotation=True), headers=free), "rotation")
    _is_402(client.get(f"{V1}/reports/tasks", headers=free), "statistiques")
    _is_402(client.get(f"{V1}/savings", headers=free), "Épargne")
    assert client.get(f"{V1}/reports/finances", headers=free).status_code == 200  # current month: free
    _is_402(client.get(f"{V1}/reports/finances", params={"month": "2020-01"}, headers=free), "mois passés")
    assert client.get(f"{V1}/reports/daily", headers=free).status_code == 200  # daily report: free


def test_module_links_need_premium_complete(client, free):
    acc = client.post(f"{V1}/accounts", json={"name": "Wave", "type": "wave", "balance": 50000}, headers=free).json()
    hid = client.get(f"{V1}/auth/me", headers=free).json()["household_id"]
    with SessionLocal() as db:
        db.add(Bill(id="b-free", household_id=hid, label="Eau", amount=9000, due_date=clock.today()))
        db.commit()
    r = client.post(f"{V1}/deadlines/bills/b-free/pay", headers=free)
    assert r.status_code == 200 and r.json()["operation_id"] is None  # paid, but no automatic expense
    assert client.get(f"{V1}/accounts", headers=free).json()[0]["balance"] == acc["balance"]
    _is_402(client.post(f"{V1}/shopping-lists/expense", headers=free), "liste de courses")


def test_trial_once(client, free):
    r = client.post(f"{V1}/subscriptions/trial", headers=free).json()
    assert (r["is_premium"], r["premium_source"], r["trial_used"]) == (True, "trial", True)
    assert client.get(f"{V1}/reports/tasks", headers=free).status_code == 200
    assert client.post(f"{V1}/subscriptions/trial", headers=free).status_code == 400
    hid = client.get(f"{V1}/auth/me", headers=free).json()["household_id"]
    with SessionLocal() as db:  # 8 days later
        db.get(Household, hid).premium_until = clock.now().replace(year=2020)
        db.commit()
    assert client.get(f"{V1}/subscriptions/current", headers=free).json()["is_premium"] is False
    assert client.post(f"{V1}/subscriptions/trial", headers=free).status_code == 400  # still only once


def test_revenuecat_webhook(client, free, monkeypatch):
    hid = client.get(f"{V1}/auth/me", headers=free).json()["household_id"]
    event = {"event": {"type": "INITIAL_PURCHASE", "app_user_id": hid, "product_id": "foyo_finances_yearly",
                       "expiration_at_ms": int((time.time() + 365 * 86400) * 1000)}}
    assert client.post(f"{V1}/webhooks/revenuecat", json=event).status_code == 503  # not configured
    monkeypatch.setattr(get_settings(), "revenuecat_webhook_secret", "s3cret")
    assert client.post(f"{V1}/webhooks/revenuecat", json=event, headers={"Authorization": "nope"}).status_code == 401
    assert client.post(f"{V1}/webhooks/revenuecat", json=event, headers={"Authorization": "s3cret"}).json()["applied"]
    cur = client.get(f"{V1}/subscriptions/current", headers=free).json()
    assert (cur["is_premium"], cur["plan"], cur["premium_period"], cur["will_renew"]) == (True, "finances", "yearly", True)

    cancel = {"event": {**event["event"], "type": "CANCELLATION"}}
    client.post(f"{V1}/webhooks/revenuecat", json=cancel, headers={"Authorization": "s3cret"})
    cur = client.get(f"{V1}/subscriptions/current", headers=free).json()
    assert (cur["is_premium"], cur["will_renew"]) == (True, False)  # paid period goes on

    expired = {"event": {**event["event"], "type": "EXPIRATION", "expiration_at_ms": int((time.time() - 60) * 1000)}}
    client.post(f"{V1}/webhooks/revenuecat", json=expired, headers={"Authorization": "s3cret"})
    assert client.get(f"{V1}/subscriptions/current", headers=free).json()["is_premium"] is False
    unknown = {"event": {"type": "INITIAL_PURCHASE", "app_user_id": "h-nope", "product_id": "x"}}
    assert client.post(f"{V1}/webhooks/revenuecat", json=unknown, headers={"Authorization": "s3cret"}).json()["applied"] is False
