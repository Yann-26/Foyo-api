from datetime import datetime, time, timedelta

from app.application.upcoming import upcoming
from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Member, User
from app.workers import reminder_jobs
from tests.integration.test_notifications import AWA_TOKEN, pushes  # noqa: F401  (pushes is a fixture)

V1 = "/api/v1"


def _upcoming(email: str, hh: int, mm: int, days: int = 7) -> list[dict]:
    now = datetime.combine(clock.today(), time(hh, mm))
    with SessionLocal() as db:
        user = db.query(User).filter(User.email == email).one()
        me = db.query(Member).filter(Member.user_id == user.id, Member.active).one()
        return upcoming(db, user, me, days=days, now=now)


def test_endpoint_returns_utc_instants_and_ready_texts(client, awa):
    r = client.get(f"{V1}/reminders/upcoming", headers=awa)
    assert r.status_code == 200
    for item in r.json():
        assert item["at"].endswith("Z") and item["kind"] in ("task", "medication")
        assert item["title"] and item["body"] and item["route"].startswith("/")


def test_awa_gets_her_tasks_and_juniors_medications_but_not_others_tasks(client):
    items = _upcoming("awa.kouassi@email.com", 6, 0, days=1)
    titles = [i["title"] for i in items]
    assert "Préparer le dîner" in titles  # Awa's own daily task
    assert "Médicament à 13:00 — Junior" in titles  # Junior's profile is managed by Awa
    assert "Médicament à 7:30 — Koffi" not in titles and "Médicament à 07:30 — Koffi" not in titles
    assert "Vaisselle du soir" not in titles  # Inès's task


def test_past_done_and_taken_are_skipped(client, awa):
    items = _upcoming("awa.kouassi@email.com", 12, 0, days=0)
    keys = {i["key"] for i in items}
    today = clock.today()
    assert f"med:i1:{today}" not in keys  # Junior's 8:00 syrup: past, and already confirmed
    assert f"med:i2:{today}" in keys  # 13:00 Vitamine D: still to come
    client.post(f"{V1}/tasks/t-diner/complete", json={"actual_seconds": 1800}, headers=awa)
    assert f"task:t-diner:{today}" not in {i["key"] for i in _upcoming("awa.kouassi@email.com", 12, 0, days=0)}


def test_quiet_hours_and_push_switch_apply(client, awa):
    client.post(
        f"{V1}/tasks",
        json={"name": "Fermer le portail", "category_id": "menage", "estimated_minutes": 2, "time": "23:00", "assignee_id": "m-awa"},
        headers=awa,
    )
    assert "Fermer le portail" not in {i["title"] for i in _upcoming("awa.kouassi@email.com", 6, 0, days=0)}
    client.patch(f"{V1}/notifications/settings", json={"quiet_hours": False}, headers=awa)
    assert "Fermer le portail" in {i["title"] for i in _upcoming("awa.kouassi@email.com", 6, 0, days=0)}
    client.patch(f"{V1}/notifications/settings", json={"push_enabled": False}, headers=awa)
    assert _upcoming("awa.kouassi@email.com", 6, 0) == []


def test_capped_for_ios_and_sorted(client):
    items = _upcoming("awa.kouassi@email.com", 0, 1, days=14)
    assert len(items) <= 60
    assert [i["at"] for i in items] == sorted(i["at"] for i in items)


def test_phone_scheduling_locally_gets_no_server_copy(client, awa, pushes):
    client.post(f"{V1}/devices", json={"token": AWA_TOKEN, "platform": "android", "local_reminders": True}, headers=awa)
    now = datetime.combine(clock.today(), time(18, 31))
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, now) == 0  # dinner: the phone fires it itself
    client.post(f"{V1}/notifications/test", headers=awa)  # other kinds still arrive
    assert len(pushes) == 1
    # Same phone without local scheduling (e.g. an older app version): the server sends it.
    client.post(f"{V1}/devices", json={"token": AWA_TOKEN, "platform": "android"}, headers=awa)
    with SessionLocal() as db:
        assert reminder_jobs.task_reminders(db, now + timedelta(minutes=1)) >= 1
