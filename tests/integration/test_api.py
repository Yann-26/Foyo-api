from tests.conftest import login

V1 = "/api/v1"


def test_auth_required(client):
    assert client.get(f"{V1}/tasks/today").status_code == 401


def test_sign_up_creates_household_and_rejects_duplicates(client):
    body = {"first_name": "Fatou", "email": "fatou@example.test", "password": "secret123"}
    r = client.post(f"{V1}/auth/sign-up", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    h = {"Authorization": f"Bearer {data['access_token']}"}
    assert data["user"]["first_name"] == "Fatou"

    hh = client.get(f"{V1}/households/current", headers=h).json()
    assert hh["members"][0]["role"] == "admin"
    assert client.post(f"{V1}/auth/sign-up", json=body).status_code == 409

    r = client.post(f"{V1}/households", json={"name": "Famille Diallo", "invitations": [{"email": "a@b.test", "role": "adult"}]}, headers=h)
    assert r.status_code == 201
    assert r.json()["name"] == "Famille Diallo"
    assert r.json()["invitations"][0]["sent_label"] == "Envoyée à l’instant"


def test_wrong_password(client):
    r = client.post(f"{V1}/auth/sign-in", json={"email": "awa.kouassi@email.com", "password": "nope"})
    assert r.status_code == 401


def test_every_screen_endpoint_answers(client, awa):
    for path in [
        "/auth/me", "/households/current", "/members", "/tasks/today", "/tasks", "/categories",
        "/categories/cuisine", "/categories/menage", "/reports/daily", "/reports/tasks?period=week",
        "/reports/tasks?period=month", "/reports/tasks?period=year", "/reports/load", "/health", "/deadlines",
        "/dishes", "/dishes/d-kedjenou", "/meal-plans", "/shopping-lists", "/finances/summary", "/accounts",
        "/operations", "/budgets", "/reports/finances", "/savings", "/subscriptions/plans", "/subscriptions/current",
    ]:
        r = client.get(V1 + path, headers=awa)
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


def test_today_and_complete(client, awa):
    today = client.get(f"{V1}/tasks/today", headers=awa).json()
    assert today["summary"]["household_planned"] == len(today["tasks"]) > 0
    todo = next(t for t in today["tasks"] if t["status"] == "todo")
    r = client.post(f"{V1}/tasks/{todo['id']}/complete", json={"actual_seconds": 600}, headers=awa)
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["task"]["status"] == "done" and c["task"]["actual_minutes"] == 10
    assert c["points_earned"] == 10
    after = client.get(f"{V1}/tasks/today", headers=awa).json()
    assert after["summary"]["household_done"] == today["summary"]["household_done"] + 1


def test_task_validation(client, awa):
    base = {"name": "Arroser", "category_id": "menage", "estimated_minutes": 10, "recurrence": {"kind": "daily"}}
    assert client.post(f"{V1}/tasks", json=base, headers=awa).status_code == 201
    assert client.post(f"{V1}/tasks", json={**base, "name": "x" * 61}, headers=awa).status_code == 422
    assert client.post(f"{V1}/tasks", json={**base, "estimated_minutes": 481}, headers=awa).status_code == 422
    steps = [{"label": f"s{i}"} for i in range(11)]
    assert client.post(f"{V1}/tasks", json={**base, "sub_steps": steps}, headers=awa).status_code == 422


def test_expense_updates_account_budget_and_totals(client, awa):
    before = client.get(f"{V1}/finances/summary", headers=awa).json()
    wave = next(a for a in before["accounts"] if a["id"] == "a-wave")
    food = next(b for b in client.get(f"{V1}/budgets", headers=awa).json() if b["category_id"] == "alimentation")
    r = client.post(
        f"{V1}/operations",
        json={"type": "expense", "amount": 5000, "category_id": "alimentation", "account_id": "a-wave"},
        headers=awa,
    )
    assert r.status_code == 201, r.text
    after = client.get(f"{V1}/finances/summary", headers=awa).json()
    assert next(a for a in after["accounts"] if a["id"] == "a-wave")["balance"] == wave["balance"] - 5000
    assert after["expense"] == before["expense"] + 5000
    food2 = next(b for b in client.get(f"{V1}/budgets", headers=awa).json() if b["category_id"] == "alimentation")
    assert food2["spent"] == food["spent"] + 5000


def test_transfer_is_neutral(client, awa):
    before = client.get(f"{V1}/finances/summary", headers=awa).json()
    r = client.post(
        f"{V1}/operations",
        json={"type": "transfer", "amount": 1000, "category_id": "autre", "account_id": "a-bank", "to_account_id": "a-cash"},
        headers=awa,
    )
    assert r.status_code == 201
    after = client.get(f"{V1}/finances/summary", headers=awa).json()
    assert (after["income"], after["expense"]) == (before["income"], before["expense"])
    assert after["household_balance"] == before["household_balance"]


def test_private_account_only_visible_to_owner(client, awa, koffi):
    assert "a-koffi-perso" not in {a["id"] for a in client.get(f"{V1}/accounts", headers=awa).json()}
    assert "a-koffi-perso" in {a["id"] for a in client.get(f"{V1}/accounts", headers=koffi).json()}
    r = client.post(
        f"{V1}/operations",
        json={"type": "expense", "amount": 10, "category_id": "autre", "account_id": "a-koffi-perso"},
        headers=awa,
    )
    assert r.status_code == 400


def test_pay_bill_creates_expense_in_complete_plan(client, awa):
    before = client.get(f"{V1}/finances/summary", headers=awa).json()["expense"]
    r = client.post(f"{V1}/deadlines/bills/b2/pay", headers=awa)
    assert r.status_code == 200, r.text
    assert r.json()["paid"] and r.json()["operation_id"]
    assert client.get(f"{V1}/finances/summary", headers=awa).json()["expense"] == before + 9200


def test_plan_gating(client, awa):
    assert client.put(f"{V1}/subscriptions/current", json={"plan": "tasks"}, headers=awa).json()["plan"] == "tasks"
    assert client.get(f"{V1}/accounts", headers=awa).status_code == 403
    assert client.get(f"{V1}/tasks/today", headers=awa).status_code == 200


def test_health_toggle(client, awa):
    r = client.post(f"{V1}/health/intakes/i5/toggle", headers=awa)
    assert r.status_code == 200 and r.json()["taken"] is True
    assert client.post(f"{V1}/health/intakes/i5/toggle", headers=awa).json()["taken"] is False


def test_koffi_login(client):
    assert login(client, "koffi.k@email.com")


def test_admin_transfer(client, awa, koffi):
    # Inès has no account in the demo data: she can't become admin.
    r = client.post(f"{V1}/households/current/transfer-admin", json={"member_id": "m-ines"}, headers=awa)
    assert r.status_code == 400 and "n’a pas de compte" in r.json()["detail"]
    assert client.post(f"{V1}/households/current/transfer-admin", json={"member_id": "m-awa"}, headers=awa).status_code == 400
    assert client.post(f"{V1}/households/current/transfer-admin", json={"member_id": "m-awa"}, headers=koffi).status_code == 403

    r = client.post(f"{V1}/households/current/transfer-admin", json={"member_id": "m-koffi"}, headers=awa)
    assert r.status_code == 200, r.text
    roles = {m["id"]: m["role"] for m in r.json()["members"]}
    assert roles["m-koffi"] == "admin" and roles["m-awa"] == "adult"
    assert {m["id"]: m["has_account"] for m in r.json()["members"]}["m-ines"] is False
    # Awa lost admin rights, Koffi has them.
    assert client.put(f"{V1}/subscriptions/current", json={"plan": "tasks"}, headers=awa).status_code == 403
    assert client.put(f"{V1}/subscriptions/current", json={"plan": "complete"}, headers=koffi).status_code == 200


def test_sign_in_locks_after_repeated_failures(client):
    bad = {"email": "awa.kouassi@email.com", "password": "mauvais-mdp"}
    for _ in range(5):
        assert client.post(f"{V1}/auth/sign-in", json=bad).status_code == 401
    good = {"email": "awa.kouassi@email.com", "password": "foyo1234"}
    r = client.post(f"{V1}/auth/sign-in", json=good)
    assert r.status_code == 429 and "Réessayez dans" in r.json()["detail"]
    # Unknown e-mails get the same 401 as wrong passwords.
    assert client.post(f"{V1}/auth/sign-in", json={"email": "x@example.test", "password": "y"}).status_code == 401


def test_production_refuses_unsafe_settings(monkeypatch):
    monkeypatch.delenv("SEED_DEMO", raising=False)
    import pytest as _pytest

    from app.core.config import Settings

    with _pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(environment="production", _env_file=None)
    with _pytest.raises(ValueError, match="SEED_DEMO"):
        Settings(environment="production", jwt_secret="x" * 40, seed_demo=True, _env_file=None)
    with _pytest.raises(ValueError, match="CORS"):
        Settings(environment="production", jwt_secret="x" * 40, cors_origins=["*"], _env_file=None)
    ok = Settings(environment="production", jwt_secret="x" * 40, _env_file=None)
    assert ok.seed_demo is False and ok.cors_origins == []
    assert Settings(environment="development", _env_file=None).seed_demo is True


def test_ticked_without_timer_uses_the_estimate(client, awa):
    r = client.post(f"{V1}/tasks/t-diner/complete", json={}, headers=awa)
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["actual_minutes"] == 45 and task["duration_estimated"] is True
    timed = client.post(f"{V1}/tasks/t-bain/complete", json={"actual_seconds": 600}, headers=awa).json()["task"]
    assert timed["duration_estimated"] is False
