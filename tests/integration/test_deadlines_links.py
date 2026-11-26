"""Links and deadlines (spec F11, §5.11): recurring bills, mileage, maintenance cost, low stock → shopping
list, appointment and J-7 bill reminders, overspent budget in the daily report."""

from datetime import datetime, timedelta

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Appointment, Bill, MedicationStock
from app.workers import reminder_jobs
from tests.integration.test_notifications import AWA_TOKEN, KOFFI_TOKEN, _to, at, phones, pushes  # noqa: F401

V1 = "/api/v1"


def test_recurring_bill_comes_back_when_paid(client, awa):
    today = clock.today()
    b = client.post(
        f"{V1}/deadlines/bills",
        json={"label": "Canal+", "amount": 10000, "due_date": (today + timedelta(days=2)).isoformat(), "every_months": 1},
        headers=awa,
    ).json()
    assert b["every_months"] == 1
    client.post(f"{V1}/deadlines/bills/{b['id']}/pay", headers=awa)
    with SessionLocal() as db:
        nxt = db.query(Bill).filter(Bill.label == "Canal+", Bill.paid_at.is_(None)).one()
        assert nxt.due_date.month != (today + timedelta(days=2)).month or nxt.due_date.year != today.year
        assert nxt.every_months == 1
    one_off = client.post(
        f"{V1}/deadlines/bills", json={"label": "Mariage", "amount": 50000, "due_date": today.isoformat()}, headers=awa
    ).json()
    assert client.delete(f"{V1}/deadlines/bills/{one_off['id']}", headers=awa).status_code == 204


def test_mileage_and_maintenance_cost(client, awa):
    before = client.get(f"{V1}/finances/summary", headers=awa).json()["expense"]
    r = client.put(f"{V1}/deadlines/vehicles/v1/mileage", json={"mileage": 84000}, headers=awa)
    assert r.status_code == 400 and "84 300" in r.json()["detail"]
    v = client.put(f"{V1}/deadlines/vehicles/v1/mileage", json={"mileage": 84900}, headers=awa).json()["vehicle"]
    vidange = next(i for i in v["items"] if i["label"] == "Vidange")
    assert vidange["badge"] == "Bientôt"  # 100 km left
    v = client.post(
        f"{V1}/deadlines/maintenances/{vidange['id']}/done", json={"cost": 35000, "mileage": 85050}, headers=awa
    ).json()["vehicle"]
    vidange = next(i for i in v["items"] if i["label"] == "Vidange")
    assert (vidange["last_cost"], vidange["urgent"], v["mileage"]) == (35000, False, 85050)
    # Complet plan: the cost is a Transport expense.
    assert client.get(f"{V1}/finances/summary", headers=awa).json()["expense"] == before + 35000


def test_low_stock_goes_to_the_shopping_list_once(client, awa):
    with SessionLocal() as db:
        db.get(MedicationStock, "st2").remaining = 8  # Sirop, alert at 7
        db.commit()
    client.post(f"{V1}/health/intakes/i3/toggle", params={"taken": "true"}, headers=awa)  # 8 → 7: alert level
    items = client.get(f"{V1}/shopping-lists", headers=awa).json()["items"]
    sirop = [i for i in items if i["name"] == "Sirop"]
    assert len(sirop) == 1 and sirop[0]["source"] == "Stock santé bas"
    stocks = client.get(f"{V1}/health", headers=awa).json()["stock_alerts"]
    assert next(s for s in stocks if s["product"] == "Sirop")["in_shopping_list"] is True
    # The button on a low stock doesn't add it twice.
    r = client.post(f"{V1}/health/stocks/st2/shopping", headers=awa).json()
    assert r == {"added": False, "in_shopping_list": True}
    vit = client.post(f"{V1}/health/stocks/st1/shopping", headers=awa).json()
    assert vit["added"] is True or vit["in_shopping_list"] is True


def test_appointment_reminders_evening_before_and_2h_before(client, awa, pushes, phones):  # noqa: F811
    tomorrow_10 = datetime.combine(clock.today() + timedelta(days=1), datetime.min.time()).replace(hour=10)
    with SessionLocal() as db:
        db.add(Appointment(id="ap-test", household_id="h-kouassi", member_id="m-junior", title="Pédiatre", at=tomorrow_10))
        db.commit()
        reminder_jobs.appointment_reminders(db, at(18, 31))
        reminder_jobs.appointment_reminders(db, at(18, 45))
        reminder_jobs.appointment_reminders(db, tomorrow_10 - timedelta(hours=2) + timedelta(minutes=1))
    titles = [m.title for m in _to(pushes, AWA_TOKEN)]  # Junior's profile is managed by Awa
    assert titles.count("Demain 10:00 · Pédiatre (Junior)") == 1
    assert titles.count("Dans 2 h · Pédiatre (Junior)") == 1
    assert not [m for m in _to(pushes, KOFFI_TOKEN) if "Pédiatre" in m.title]


def test_bill_reminder_a_week_before(client, awa, pushes, phones):  # noqa: F811
    with SessionLocal() as db:
        db.add(Bill(id="b-week", household_id="h-kouassi", label="Assurance", amount=60000,
                    due_date=clock.today() + timedelta(days=7)))
        db.commit()
        reminder_jobs.bill_reminders(db, at(18, 31))
    assert any(m.title == "Facture assurance dans 7 jours" for m in _to(pushes, AWA_TOKEN))


def test_overspent_budget_in_the_daily_report(client, awa, koffi, pushes, phones):  # noqa: F811
    client.put(f"{V1}/budgets", json={"category_id": "habillement", "planned": 1000}, headers=awa)
    client.post(
        f"{V1}/operations", json={"type": "expense", "amount": 3000, "category_id": "habillement", "account_id": "a-cash"},
        headers=awa,
    )
    report = client.get(f"{V1}/reports/daily", headers=awa).json()
    assert report["budget_alert"].startswith("Budget dépassé :") and "+2 000 F" in report["budget_alert"]
    with SessionLocal() as db:
        reminder_jobs.daily_reports(db, at(21, 1))
    daily = [m for m in _to(pushes, AWA_TOKEN) if m.title == "Rapport du jour"]
    assert daily and "Budget dépassé" in daily[-1].body
