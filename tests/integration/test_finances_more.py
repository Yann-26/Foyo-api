"""Finances (spec F15–F19): fees, recurring operations, receipts, budget alerts, repayments, tontines,
quarter / year reports, CSV export."""

from datetime import date, timedelta

from app.application import finance_ops
from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Operation, RecurringOperation
from app.workers import reminder_jobs
from tests.integration.test_notifications import AWA_TOKEN, KOFFI_TOKEN, _to, at, phones, pushes  # noqa: F401

V1 = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _balance(client, h, acc):
    return next(a for a in client.get(f"{V1}/accounts", headers=h).json() if a["id"] == acc)["balance"]


def test_transfer_fee_is_a_separate_expense_deleted_with_it(client, awa):
    wave, cash = _balance(client, awa, "a-wave"), _balance(client, awa, "a-cash")
    r = client.post(
        f"{V1}/operations",
        json={"type": "transfer", "amount": 10000, "fee": 100, "category_id": "autre", "account_id": "a-wave",
              "to_account_id": "a-cash"},
        headers=awa,
    )
    assert r.status_code == 201, r.text
    assert (_balance(client, awa, "a-wave"), _balance(client, awa, "a-cash")) == (wave - 10100, cash + 10000)
    ops = client.get(f"{V1}/operations", headers=awa).json()
    fee = next(o for o in ops if o["parent_id"] == r.json()["id"])
    assert (fee["type"], fee["amount"], fee["category_id"], fee["origin"]) == ("expense", 100, "frais", "fee")
    assert client.delete(f"{V1}/operations/{r.json()['id']}", headers=awa).status_code == 204
    assert (_balance(client, awa, "a-wave"), _balance(client, awa, "a-cash")) == (wave, cash)


def test_receipt_photo(client, awa, koffi):
    op = client.post(
        f"{V1}/operations",
        json={"type": "expense", "amount": 3000, "category_id": "alimentation", "account_id": "a-cash"},
        headers=awa,
    ).json()
    r = client.post(f"{V1}/operations/{op['id']}/receipt", files={"file": ("r.png", PNG, "image/png")}, headers=awa)
    assert r.status_code == 200 and r.json()["has_receipt"]
    assert client.get(f"{V1}/operations/{op['id']}/receipt", headers=koffi).content == PNG
    # Private account of Koffi: Awa can't attach anything to it.
    priv = client.post(
        f"{V1}/operations",
        json={"type": "expense", "amount": 10, "category_id": "autre", "account_id": "a-koffi-perso"},
        headers=koffi,
    ).json()
    assert client.post(f"{V1}/operations/{priv['id']}/receipt", files={"file": ("r.png", PNG, "image/png")}, headers=awa).status_code == 404


def test_recurring_operation_is_written_on_its_day_and_catches_up(client, awa, pushes, phones):  # noqa: F811
    r = client.post(
        f"{V1}/recurring-operations",
        json={"type": "expense", "amount": 20000, "category_id": "scolarite", "account_id": "a-bank",
              "label": "Cantine", "day": 31, "every_months": 1},
        headers=awa,
    ).json()
    first = date.fromisoformat(r["next_date"])
    assert first >= clock.today()
    assert first.day == min(31, finance_ops.due_date(first.year, first.month, 31).day)  # last day of short months
    with SessionLocal() as db:
        # Worker down for two months: both are written, the next one is scheduled.
        later = finance_ops.after(db.get(RecurringOperation, r["id"]), first)
        written = finance_ops.run_recurring(db, later)
        assert len([1 for rec, _ in written if rec.id == r["id"]]) == 2
        assert db.get(RecurringOperation, r["id"]).next_date > later
    with SessionLocal() as db:
        cantine = db.query(Operation).filter(Operation.label == "Cantine", Operation.origin == "recurring").all()
        assert len(cantine) == 2 and all(o.amount == 20000 and o.account_id == "a-bank" for o in cantine)
    assert client.delete(f"{V1}/recurring-operations/{r['id']}", headers=awa).status_code == 204


def test_repeat_flag_on_a_new_operation(client, awa):
    client.post(
        f"{V1}/operations",
        json={"type": "income", "amount": 5000, "category_id": "commerce", "account_id": "a-cash",
              "label": "Loyer boutique", "repeat_every_months": 1},
        headers=awa,
    )
    rec = [x for x in client.get(f"{V1}/recurring-operations", headers=awa).json() if x["label"] == "Loyer boutique"]
    assert len(rec) == 1 and date.fromisoformat(rec[0]["next_date"]) > clock.today()


def test_budget_alerts_at_80_and_over_once(client, awa, pushes, phones):  # noqa: F811
    client.put(f"{V1}/budgets", json={"category_id": "loisirs", "planned": 10000}, headers=awa)
    def alerts():
        return [m.title for m in _to(pushes, AWA_TOKEN) if m.title.startswith("Budget Loisirs")]

    with SessionLocal() as db:
        spent = finance_ops.budget_state(db, "h-kouassi", "loisirs", clock.now())
    already = spent[1] if spent else 0
    def spend(n):
        assert client.post(
            f"{V1}/operations", json={"type": "expense", "amount": n, "category_id": "loisirs", "account_id": "a-cash"},
            headers=awa,
        ).status_code == 201

    if already < 8000:
        spend(8000 - already)
        assert alerts()[-1] == "Budget Loisirs à 80 %"
        spend(100)
        assert alerts().count("Budget Loisirs à 80 %") == 1
    spend(5000)
    assert alerts()[-1] == "Budget Loisirs dépassé" and KOFFI_TOKEN in [t for ts, m in pushes if m.title == "Budget Loisirs dépassé" for t in ts]


def test_partial_debt_repayment_writes_the_money(client, awa):
    cash = _balance(client, awa, "a-cash")
    d = client.post(f"{V1}/savings/debts/d1/repay", json={"amount": 20000, "account_id": "a-cash"}, headers=awa).json()
    assert (d["repaid"], d["remaining"]) == (20000, 30000)
    assert _balance(client, awa, "a-cash") == cash + 20000  # lent to Yao: money comes back
    r = client.post(f"{V1}/savings/debts/d1/repay", json={"amount": 30001}, headers=awa)
    assert r.status_code == 400 and "30000" in r.json()["detail"]
    d2 = client.post(f"{V1}/savings/debts/d2/repay", json={"amount": 60000, "account_id": "a-cash"}, headers=awa).json()
    assert d2["remaining"] == 0 and _balance(client, awa, "a-cash") == cash + 20000 - 60000


def test_tontine_create_pay_and_reminders(client, awa, pushes, phones):  # noqa: F811
    today = clock.today()
    t = client.post(
        f"{V1}/savings/tontines",
        json={"name": "Tontine des voisines", "contribution": 10000, "member_count": 5, "my_turn": 1,
              "start_date": today.isoformat(), "pay_day": (today + timedelta(days=1)).day if today.day < 28 else 28},
        headers=awa,
    )
    assert t.status_code == 201, t.text
    paid = client.post(f"{V1}/savings/tontines/{t.json()['id']}/pay", json={"account_id": "a-cash"}, headers=awa).json()
    assert (paid["category_id"], paid["origin"], paid["amount"]) == ("tontine", "tontine", 10000)
    if today.day < 28:  # the contribution day is tomorrow
        with SessionLocal() as db:
            reminder_jobs.tontine_reminders(db, at(18, 31))
            reminder_jobs.tontine_reminders(db, at(18, 40))
        msgs = [m for m in _to(pushes, AWA_TOKEN) if m.title == "Tontine Tontine des voisines : cotisation demain"]
        assert len(msgs) == 1 and "C’est votre tour" in msgs[0].body


def test_quarter_and_year_reports_and_csv(client, awa, koffi):
    month = client.get(f"{V1}/reports/finances", headers=awa).json()
    quarter = client.get(f"{V1}/reports/finances?period=quarter", headers=awa).json()
    year = client.get(f"{V1}/reports/finances?period=year", headers=awa).json()
    assert quarter["expense"] >= month["expense"] and year["expense"] >= quarter["expense"]
    assert len(quarter["months"]) == 3 and len(year["months"]) == 12
    assert "trimestre" in quarter["period_label"] and year["period_label"].startswith("Année")

    r = client.get(f"{V1}/operations/export.csv", headers=awa)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8")
    assert text.startswith("﻿Date;Type;Catégorie") and "Marché de Cocody" in text
    assert "a-koffi-perso" not in text
    assert client.get(f"{V1}/operations/export.csv?period=year", headers=awa).status_code == 200
