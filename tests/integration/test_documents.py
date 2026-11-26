from datetime import date, timedelta

from app.core import clock

V1 = "/api/v1"


def test_document_data_for_week_and_month(client, awa):
    r = client.get(f"{V1}/documents/data", headers=awa)
    assert r.status_code == 200, r.text
    d = r.json()
    monday = clock.today() - timedelta(days=clock.today().weekday())
    assert d["household_name"] == "Famille Kouassi"
    assert d["week"]["start"] == monday.isoformat()
    assert d["week"]["label"].startswith("Semaine du ")
    days = {e["date"] for e in d["week"]["entries"]}
    assert days == {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    dinner = [e for e in d["week"]["entries"] if e["task_id"] == "t-diner"]
    assert len(dinner) == 7 and all(e["assignee_id"] == "m-awa" for e in dinner)  # daily, Awa
    assert len(d["week"]["menu"]) == 20  # the seeded week menu
    assert len(d["month"]["days"]) in (28, 29, 30, 31)
    assert any(i["kind"] == "bill" for day in d["month"]["days"] for i in day["items"])


def test_rotation_shows_each_days_assignee(client, awa):
    entries = client.get(f"{V1}/documents/data", headers=awa).json()["week"]["entries"]
    trash = {e["assignee_id"] for e in entries if e["task_id"] == "t-poubelles"}
    assert len(trash) >= 2  # rotating task: not always the same person


def test_other_week_and_month(client, awa):
    nxt = clock.today() + timedelta(days=7)
    r = client.get(f"{V1}/documents/data", params={"week": nxt.isoformat(), "month": "2027-02"}, headers=awa)
    d = r.json()
    assert date.fromisoformat(d["week"]["start"]).weekday() == 0
    assert d["month"]["label"] == "Février 2027" and len(d["month"]["days"]) == 28


def test_teens_cannot_export(client):
    """Spec §5.2: no PDF export for teens and children (see also test_role_visibility.py)."""
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Ado", "email": "ado@example.test", "password": "secret123"})
    admin = {"Authorization": f"Bearer {r.json()['access_token']}"}
    client.post(f"{V1}/households/current/invitations", json={"email": "teen@example.test", "role": "teen"}, headers=admin)
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Teen", "email": "teen@example.test", "password": "secret123"})
    teen = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get(f"{V1}/documents/data", headers=teen).status_code == 403
