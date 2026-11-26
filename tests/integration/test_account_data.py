"""Personal data: export and account deletion."""

from app.core.database import SessionLocal
from app.infrastructure.db.models import Account, Household, Medication, Member, Operation, User
from tests.conftest import login

V1 = "/api/v1"


def _sign_up(client, email, name="Fatou"):
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": name, "email": email, "password": "secret123"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_export_contains_my_data_only(client, awa):
    r = client.get(f"{V1}/auth/me/export", headers=awa)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = r.json()
    assert data["account"]["email"] == "awa.kouassi@email.com"
    assert data["membership"]["role"] == "admin"
    assert data["tasks_done"] and all("task" in t for t in data["tasks_done"])
    assert {m["product"] for m in data["medications"]}  # Awa's iron
    assert "password_hash" not in str(data) and "token" not in str(data["devices"])
    # Koffi's private account is not Awa's data.
    assert "Perso" not in str(data["private_accounts"])


def test_delete_needs_confirmation_and_password(client):
    h = _sign_up(client, "solo@example.test")
    assert client.post(f"{V1}/auth/me/delete", json={"confirm": "oui", "password": "secret123"}, headers=h).status_code == 400
    assert client.post(f"{V1}/auth/me/delete", json={"confirm": "SUPPRIMER", "password": "nope"}, headers=h).status_code == 403


def test_deleting_the_only_account_erases_the_household(client):
    h = _sign_up(client, "solo2@example.test")
    hid = client.get(f"{V1}/auth/me", headers=h).json()["household_id"]
    assert client.post(f"{V1}/auth/me/delete", json={"confirm": "supprimer", "password": "secret123"}, headers=h).status_code == 204
    assert client.get(f"{V1}/auth/me", headers=h).status_code == 401
    with SessionLocal() as db:
        assert db.get(Household, hid) is None
        assert db.query(Member).filter(Member.household_id == hid).count() == 0
    assert client.post(f"{V1}/auth/sign-in", json={"email": "solo2@example.test", "password": "secret123"}).status_code == 401


def test_leaving_member_is_anonymised_and_admin_passes_on(client, awa, koffi):
    r = client.post(f"{V1}/auth/me/delete", json={"confirm": "SUPPRIMER", "password": "foyo1234"}, headers=awa)
    assert r.status_code == 204, r.text
    with SessionLocal() as db:
        assert db.query(User).filter(User.email == "awa.kouassi@email.com").count() == 0
        awa_m = db.get(Member, "m-awa")
        assert (awa_m.first_name, awa_m.user_id, awa_m.active, awa_m.email) == ("Ancien membre", None, False, None)
        assert db.get(Member, "m-koffi").role == "admin"
        assert db.query(Medication).filter(Medication.member_id == "m-awa").count() == 0
        # Shared operations Awa entered stay in the household's history.
        assert db.query(Operation).filter(Operation.author_id == "m-awa").count() > 0
        assert db.query(Account).filter(Account.owner_id == "m-awa", Account.is_private).count() == 0
    hh = client.get(f"{V1}/households/current", headers=koffi).json()
    assert "Awa" not in [m["first_name"] for m in hh["members"]]
    assert login(client, "koffi.k@email.com")


def test_health_needs_explicit_consent(client, awa):
    h = _sign_up(client, "sante@example.test", "Aya")
    r = client.get(f"{V1}/health", headers=h)
    assert r.status_code == 428 and "santé" in r.json()["detail"]
    assert client.get(f"{V1}/auth/me", headers=h).json()["user"]["health_consent"] is False
    assert client.post(f"{V1}/health/consent", headers=h).json() == {"health_consent": True}
    assert client.get(f"{V1}/health", headers=h).status_code == 200

    # Awa (demo) consented; withdrawing with erase removes her own medications only.
    assert client.delete(f"{V1}/health/consent?erase=true", headers=awa).json() == {"health_consent": False}
    assert client.get(f"{V1}/health", headers=awa).status_code == 428
    with SessionLocal() as db:
        assert db.query(Medication).filter(Medication.member_id == "m-awa").count() == 0
        assert db.query(Medication).filter(Medication.member_id != "m-awa").count() > 0


def test_deleting_removes_receipt_photos(client):
    from pathlib import Path

    from app.infrastructure.storage import files

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    h = _sign_up(client, "recus@example.test")
    acc = client.post(f"{V1}/accounts", json={"name": "Caisse", "type": "cash", "balance": 1000}, headers=h).json()
    op = client.post(
        f"{V1}/operations", json={"type": "expense", "amount": 10, "category_id": "autre", "account_id": acc["id"]},
        headers=h,
    ).json()
    assert client.post(f"{V1}/operations/{op['id']}/receipt", files={"file": ("r.png", png, "image/png")}, headers=h).status_code == 200
    hid = client.get(f"{V1}/auth/me", headers=h).json()["household_id"]
    folder = Path(files.root()) / "receipts" / hid
    assert any(folder.iterdir())
    assert client.post(f"{V1}/auth/me/delete", json={"confirm": "SUPPRIMER", "password": "secret123"}, headers=h).status_code == 204
    assert not folder.exists()
