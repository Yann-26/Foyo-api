"""Timer rules (spec §5.5): one running task per person, forgotten timers, note and photo at validation."""

from datetime import timedelta

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Member, Occurrence
from app.workers import reminder_jobs
from tests.integration.test_notifications import AWA_TOKEN, _to, phones, pushes  # noqa: F401

V1 = "/api/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_one_running_task_per_person(client, awa, koffi):
    assert client.post(f"{V1}/tasks/t-machine/start", headers=awa).json()["status"] == "running"
    r = client.post(f"{V1}/tasks/t-diner/start", headers=awa)
    assert r.status_code == 409 and "Lancer une machine" in r.json()["detail"]

    # Someone else can run a task at the same time.
    assert client.post(f"{V1}/tasks/t-devoirs/start", headers=koffi).status_code == 200

    stopped = client.post(f"{V1}/tasks/t-machine/stop", headers=awa).json()
    assert stopped["status"] == "todo" and stopped["elapsed_seconds"] == 0
    assert client.post(f"{V1}/tasks/t-diner/start", headers=awa).json()["status"] == "running"
    # Starting the one already running is harmless.
    assert client.post(f"{V1}/tasks/t-diner/start", headers=awa).status_code == 200


def test_note_and_photo_at_validation(client, awa, koffi):
    done = client.post(f"{V1}/tasks/t-diner/complete", json={"note": " Attiéké en plus "}, headers=awa).json()["task"]
    assert done["note"] == "Attiéké en plus" and done["has_photo"] is False
    occ = done["occurrence_id"]

    bad = client.post(f"{V1}/occurrences/{occ}/photo", files={"file": ("x.gif", b"GIF89a", "image/gif")}, headers=awa)
    assert bad.status_code == 400 and "JPEG" in bad.json()["detail"]
    big = client.post(f"{V1}/occurrences/{occ}/photo", files={"file": ("x.png", b"0" * (5 * 1024 * 1024 + 1), "image/png")}, headers=awa)
    assert big.status_code == 400

    r = client.post(f"{V1}/occurrences/{occ}/photo", files={"file": ("plat.png", PNG, "image/png")}, headers=awa)
    assert r.status_code == 200, r.text
    assert r.json()["has_photo"] is True
    got = client.get(f"{V1}/occurrences/{occ}/photo", headers=awa)
    assert got.status_code == 200 and got.content == PNG and got.headers["content-type"] == "image/png"

    edited = client.patch(f"{V1}/occurrences/{occ}", json={"note": "Plus de sacs poubelle"}, headers=awa).json()
    assert edited["note"] == "Plus de sacs poubelle" and edited["has_photo"] is True
    assert client.post(f"{V1}/tasks/t-diner/complete", json={"note": "x" * 201}, headers=awa).status_code == 422


def test_forgotten_timer_is_asked_then_stopped(client, awa, pushes, phones):  # noqa: F811
    client.post(f"{V1}/tasks/t-machine/stop", headers=awa)
    occ_id = client.post(f"{V1}/tasks/t-diner/start", headers=awa).json()["occurrence_id"]  # 45 min estimated
    now = clock.now()

    def started(ago: timedelta) -> None:
        with SessionLocal() as db:
            db.get(Occurrence, occ_id).started_at = now - ago
            db.commit()

    def titled(prefix: str):
        return [m for m in _to(pushes, AWA_TOKEN) if m.title.startswith(prefix)]

    started(timedelta(minutes=100))
    with SessionLocal() as db:
        reminder_jobs.long_timers(db, now)
    assert titled("Toujours en cours") == []  # 3 × 45 = 135 min not reached

    started(timedelta(minutes=136))
    with SessionLocal() as db:
        reminder_jobs.long_timers(db, now)
        reminder_jobs.long_timers(db, now + timedelta(minutes=5))  # once
    assert len(titled("Toujours en cours")) == 1

    with SessionLocal() as db:
        before = db.get(Member, "m-awa").points
    started(timedelta(hours=4, minutes=1))
    with SessionLocal() as db:
        reminder_jobs.long_timers(db, now)
        occ = db.get(Occurrence, occ_id)
        assert occ.status == "done" and occ.duration_estimated and occ.actual_seconds == 45 * 60
    assert titled("Chronomètre arrêté")[0].body == "Il tournait depuis 4 h. Durée retenue : 45 min (estimée)."
    with SessionLocal() as db:
        assert db.get(Member, "m-awa").points == before + 10
