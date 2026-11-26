from datetime import datetime, time, timedelta

import pytest

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Device, User
from app.infrastructure.notifications import fcm_client
from app.workers import reminder_jobs

V1 = "/api/v1"
AWA_TOKEN = "fcm-token-awa-" + "a" * 20
KOFFI_TOKEN = "fcm-token-koffi-" + "k" * 20


class Outbox(list):
    """Pushes "sent" during a test; tokens listed in `dead` are reported as uninstalled by FCM."""

    def __init__(self):
        super().__init__()
        self.dead: list[str] = []


@pytest.fixture()
def pushes(monkeypatch):
    sent = Outbox()

    def fake(tokens, msg):
        sent.append((list(tokens), msg))
        return [t for t in tokens if t in sent.dead]

    monkeypatch.setattr(fcm_client, "send_push", fake)
    return sent


@pytest.fixture()
def phones(client, awa, koffi):
    for token, h in ((AWA_TOKEN, awa), (KOFFI_TOKEN, koffi)):
        assert client.post(f"{V1}/devices", json={"token": token, "platform": "android"}, headers=h).status_code == 204


def at(hh: int, mm: int, days: int = 0) -> datetime:
    return datetime.combine(clock.today() + timedelta(days=days), time(hh, mm))


def _to(sent, token):
    return [m for tokens, m in sent if token in tokens]


def test_test_push_and_preferences(client, awa, pushes, phones):
    assert client.post(f"{V1}/notifications/test", headers=awa).status_code == 200
    assert _to(pushes, AWA_TOKEN)[-1].body == "Les notifications fonctionnent sur ce téléphone."

    r = client.patch(f"{V1}/notifications/settings", json={"push_enabled": False}, headers=awa)
    assert r.json() == {"push_enabled": False, "quiet_hours": True, "daily_report": True}
    assert client.post(f"{V1}/notifications/test", headers=awa).status_code == 400


def test_test_push_without_device(client, awa, pushes):
    assert client.post(f"{V1}/notifications/test", headers=awa).status_code == 400


def test_assignment_push_goes_to_assignee_not_to_self(client, awa, pushes, phones):
    base = {"name": "Laver la voiture", "category_id": "vehicule", "estimated_minutes": 30, "time": "10:00"}
    client.post(f"{V1}/tasks", json={**base, "assignee_id": "m-koffi"}, headers=awa)
    msg = _to(pushes, KOFFI_TOKEN)[-1]
    assert msg.title == "Nouvelle tâche : Laver la voiture" and "Awa vous l’a confiée · 10:00" in msg.body
    assert msg.data["route"] == "/today"
    n = len(pushes)
    client.post(f"{V1}/tasks", json={**base, "assignee_id": "m-awa"}, headers=awa)
    assert len(pushes) == n


def test_task_reminder_is_sent_once(client, pushes, phones):
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, at(18, 31)) >= 1  # "Préparer le dîner" (Awa, 18:30)
        assert reminder_jobs.task_reminders(db, at(18, 40)) == 0  # already sent
    msg = next(m for m in _to(pushes, AWA_TOKEN) if m.title == "Préparer le dîner")
    assert msg.body == "Awa, c’est l’heure. Durée estimée : 45 min." and msg.data["route"] == "/task/t-diner/run"


def test_late_reminders_are_dropped(client, awa, pushes, phones):
    client.post(
        f"{V1}/tasks",
        json={"name": "Arroser", "category_id": "menage", "estimated_minutes": 5, "time": "11:10", "assignee_id": "m-awa"},
        headers=awa,
    )
    with SessionLocal() as db:  # worker was down from 11:00 to 11:41: the 11:10 reminder is stale now
        assert reminder_jobs.task_reminders(db, at(11, 41)) == 0
    assert not [m for m in _to(pushes, AWA_TOKEN) if m.title == "Arroser"]


def test_quiet_hours_mute_tasks_but_not_medications(client, awa, pushes, phones):
    client.post(
        f"{V1}/tasks",
        json={"name": "Fermer le portail", "category_id": "menage", "estimated_minutes": 2, "time": "23:00", "assignee_id": "m-awa"},
        headers=awa,
    )
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, at(23, 1)) == 0
        assert reminder_jobs.medication_reminders(db, at(21, 1)) == 1  # Awa's iron at 21:00
    client.patch(f"{V1}/notifications/settings", json={"quiet_hours": False}, headers=awa)
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, at(23, 2)) == 1


def test_child_medication_goes_to_managing_admin(client, pushes, phones):
    with SessionLocal() as db:
        reminder_jobs.medication_reminders(db, at(13, 0))  # Vitamine D for Junior, not taken yet
    msg = _to(pushes, AWA_TOKEN)[-1]
    assert msg.title == "Médicament à 13:00 — Junior" and msg.body == "Vitamine D · 1 comprimé."
    assert not _to(pushes, KOFFI_TOKEN)


def test_taken_medication_is_not_reminded(client, pushes, phones):
    with SessionLocal() as db:
        assert reminder_jobs.medication_reminders(db, at(8, 5)) == 0  # Junior's 8:00 syrup was confirmed


def test_daily_report_respects_preference(client, koffi, pushes, phones):
    client.patch(f"{V1}/notifications/settings", json={"daily_report": False}, headers=koffi)
    with SessionLocal() as db:
        assert reminder_jobs.daily_reports(db, at(20, 59)) == 0
        assert reminder_jobs.daily_reports(db, at(21, 5)) == 1
    assert _to(pushes, AWA_TOKEN)[-1].title == "Rapport du jour"
    assert not _to(pushes, KOFFI_TOKEN)


def test_bill_reminders_three_days_before(client, pushes, phones):
    with SessionLocal() as db:  # Électricité is due in 4 days → reminder tomorrow at 18:30
        assert reminder_jobs.bill_reminders(db, at(18, 35)) == 0
        assert reminder_jobs.bill_reminders(db, at(18, 35, days=1)) == 2  # Awa and Koffi
    assert _to(pushes, KOFFI_TOKEN)[-1].title == "Facture électricité dans 3 jours"


def test_dead_tokens_are_removed(client, awa, pushes, phones):
    pushes.dead.append(AWA_TOKEN)
    client.post(f"{V1}/notifications/test", headers=awa)
    with SessionLocal() as db:
        assert db.query(Device).filter(Device.token == AWA_TOKEN).count() == 0


def test_sign_out_unregisters_and_phone_moves_to_new_account(client, awa, koffi, pushes, phones):
    assert client.delete(f"{V1}/devices/{AWA_TOKEN}", headers=awa).status_code == 204
    client.post(f"{V1}/devices", json={"token": KOFFI_TOKEN, "platform": "ios"}, headers=awa)  # Awa signs in on Koffi's phone
    with SessionLocal() as db:
        owner = db.query(Device).filter(Device.token == KOFFI_TOKEN).one().user_id
        assert db.get(User, owner).email == "awa.kouassi@email.com"
        assert db.query(Device).filter(Device.token == AWA_TOKEN).count() == 0


def test_task_without_time_is_reminded_at_8(client, awa, pushes, phones):
    client.post(
        f"{V1}/tasks",
        json={"name": "Arroser", "category_id": "menage", "estimated_minutes": 5, "assignee_id": "m-awa"},
        headers=awa,
    )
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, at(8, 1)) >= 1
    assert any(m.title == "Arroser" for m in _to(pushes, AWA_TOKEN))


def test_late_task_alerts_assignee_then_admin(client, koffi, pushes, phones):
    # "Devoirs de Junior" is Koffi's at 17:30 on Mon/Tue/Thu/Fri; make it due today whatever the day.
    with SessionLocal() as db:
        from app.infrastructure.db.models import Task

        t = db.get(Task, "t-devoirs")
        t.recurrence = {"kind": "daily"}
        db.commit()
    def homework(token):
        return [m for m in _to(pushes, token) if m.title == "En retard : Devoirs de Junior"]

    with SessionLocal() as db:
        reminder_jobs.late_tasks(db, at(18, 0))  # 30 min late: not yet
        assert homework(KOFFI_TOKEN) == []
        reminder_jobs.late_tasks(db, at(18, 31))  # 61 min: Koffi
        reminder_jobs.late_tasks(db, at(18, 40))  # once
        assert len(homework(KOFFI_TOKEN)) == 1 and homework(AWA_TOKEN) == []
        reminder_jobs.late_tasks(db, at(20, 31))  # 3 h: Awa (admin)
    assert len(homework(AWA_TOKEN)) == 1 and "pour Koffi" in homework(AWA_TOKEN)[0].body
    # Done in time: no alert.
    client.post(f"{V1}/tasks/t-diner/complete", json={"actual_seconds": 600}, headers=koffi)
    with SessionLocal() as db:
        reminder_jobs.late_tasks(db, at(19, 31))
    assert not [m for m in _to(pushes, AWA_TOKEN) if m.title == "En retard : Préparer le dîner"]
