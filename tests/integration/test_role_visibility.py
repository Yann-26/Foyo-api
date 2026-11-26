"""Spec §5.2: helpers only see their own tasks; teens, children and helpers see their own reports
and statistics; teens and children can't export PDFs; helpers export their own tasks only."""

import pytest

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.infrastructure.db.models import Member, User

V1 = "/api/v1"


def _account_for(client, member_id: str) -> dict:
    """Gives a seeded member without account (Adjoua, Inès) a login."""
    with SessionLocal() as db:
        m = db.get(Member, member_id)
        email = f"{member_id}@example.test"
        db.add(User(id=f"u-{member_id}", email=email, password_hash=hash_password("secret123"), first_name=m.first_name))
        db.flush()
        m.user_id = f"u-{member_id}"
        db.commit()
    r = client.post(f"{V1}/auth/sign-in", json={"email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def adjoua(client):
    return _account_for(client, "m-adjoua")  # helper


@pytest.fixture()
def ines(client):
    return _account_for(client, "m-ines")  # teen


def test_helper_documents_hold_only_her_tasks(client, adjoua, awa):
    mine = client.get(f"{V1}/documents/data", headers=adjoua).json()
    assert mine["week"]["entries"], "she has tasks this week"
    assert {e["assignee_id"] for e in mine["week"]["entries"]} == {"m-adjoua"}
    assert {m["id"] for m in mine["members"]} == {"m-adjoua"}
    assert {m["cook_id"] for m in mine["week"]["menu"]} <= {"m-adjoua"}
    assert all(d["items"] == [] for d in mine["month"]["days"])  # no bills or appointments
    everyone = client.get(f"{V1}/documents/data", headers=awa).json()
    assert len({e["assignee_id"] for e in everyone["week"]["entries"]}) > 1  # adults still see all


def test_teens_and_children_cannot_export(client, ines):
    r = client.get(f"{V1}/documents/data", headers=ines)
    assert r.status_code == 403 and "réservé aux adultes" in r.json()["detail"]


@pytest.mark.parametrize("who", ["adjoua", "ines"])
def test_reports_show_only_own_figures(client, who, request):
    h = request.getfixturevalue(who)
    me = f"m-{who}"
    daily = client.get(f"{V1}/reports/daily", headers=h).json()
    assert {m["member_id"] for m in daily["by_member"]} <= {me}
    assert daily["insight"] is None or me[2:].capitalize() in daily["insight"]
    for period in ("week", "month", "year"):
        stats = client.get(f"{V1}/reports/tasks", params={"period": period}, headers=h).json()
        assert set(stats["member_share"]) <= {me}, period
    load = client.get(f"{V1}/reports/load", headers=h).json()
    assert [row["member_id"] for row in load] == [me]


def test_category_stats_and_activity_are_own_for_a_teen(client, ines, awa):
    teen = client.get(f"{V1}/categories/cuisine", headers=ines).json()
    adult = client.get(f"{V1}/categories/cuisine", headers=awa).json()
    assert teen["done_this_week"] < adult["done_this_week"]  # Inès doesn't cook; Awa sees the household
    assert len(teen["tasks"]) == len(adult["tasks"])  # teens still see every task (spec §5.2)
    activity = {a["member_id"]: a["summary"] for a in client.get(f"{V1}/households/current", headers=ines).json()["activity"]}
    assert "tâches cette semaine" in activity["m-ines"]
    assert "tâches" not in activity["m-awa"]


def test_helper_today_totals_are_hers(client, adjoua, awa):
    hers = client.get(f"{V1}/tasks/today", headers=adjoua).json()
    assert all(t["assignee_id"] == "m-adjoua" for t in hers["tasks"])
    assert hers["summary"]["household_planned"] == hers["summary"]["my_planned"]
    household = client.get(f"{V1}/tasks/today", headers=awa).json()["summary"]["household_planned"]
    assert household > hers["summary"]["household_planned"]
