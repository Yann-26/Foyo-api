from datetime import timedelta

import pytest

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import Invitation
from app.infrastructure.email import email_client

V1 = "/api/v1"


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(email_client, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def test_invite_sends_email(client, awa, outbox):
    r = client.post(f"{V1}/households/current/invitations", json={"email": "Tante.Mariam@Example.test", "role": "adult"}, headers=awa)
    assert r.status_code == 201, r.text
    to, subject, body = outbox[-1]
    assert to == "tante.mariam@example.test"
    assert "Awa" in subject and "Famille Kouassi" in subject
    assert "en tant qu’adulte" in body and "tante.mariam@example.test" in body and "7 jours" in body


def test_create_household_emails_each_invitee(client, outbox):
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Fatou", "email": "fatou@example.test", "password": "secret123"})
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post(
        f"{V1}/households",
        json={"name": "Famille Diallo", "invitations": [
            {"email": "a@example.test", "role": "adult"}, {"email": "b@example.test", "role": "helper"},
        ]},
        headers=h,
    )
    assert r.status_code == 201
    invites = [(t, b) for t, s, b in outbox if "invite" in s]
    assert sorted(t for t, _ in invites) == ["a@example.test", "b@example.test"]
    assert "aide à domicile" in next(b for t, b in invites if t == "b@example.test")


def test_reinviting_refreshes_instead_of_duplicating(client, awa, outbox):
    for role in ("adult", "teen"):
        client.post(f"{V1}/households/current/invitations", json={"email": "cousin@example.test", "role": role}, headers=awa)
    pending = [i for i in client.get(f"{V1}/households/current", headers=awa).json()["invitations"] if i["email"] == "cousin@example.test"]
    assert len(pending) == 1 and pending[0]["role"] == "teen"


def test_existing_member_cannot_be_invited(client, awa, outbox):
    r = client.post(f"{V1}/households/current/invitations", json={"email": "koffi.k@email.com", "role": "adult"}, headers=awa)
    assert r.status_code == 409 and outbox == []


def test_resend_emails_again_but_is_throttled(client, awa, outbox):
    # The seeded invitation for mamie.rose was sent 2 days ago.
    r = client.post(f"{V1}/households/current/invitations/resend", json={"email": "mamie.rose@email.com"}, headers=awa)
    assert r.status_code == 200 and r.json()["sent_label"] == "Envoyée à l’instant"
    assert outbox[-1][0] == "mamie.rose@email.com"
    r = client.post(f"{V1}/households/current/invitations/resend", json={"email": "mamie.rose@email.com"}, headers=awa)
    assert r.status_code == 400 and len(outbox) == 1


def test_invitee_joins_on_sign_up_but_not_after_expiry(client, awa, outbox):
    client.post(f"{V1}/households/current/invitations", json={"email": "late@example.test", "role": "adult"}, headers=awa)
    client.post(f"{V1}/households/current/invitations", json={"email": "ontime@example.test", "role": "teen"}, headers=awa)
    with SessionLocal() as db:
        db.query(Invitation).filter(Invitation.email == "late@example.test").one().sent_at = clock.now() - timedelta(days=8)
        db.commit()

    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Yao", "email": "ontime@example.test", "password": "secret123"})
    assert r.json()["household_id"] == "h-kouassi"
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "Late", "email": "late@example.test", "password": "secret123"})
    assert r.json()["household_id"] != "h-kouassi"  # expired: starts their own household


# ── Existing accounts ─────────────────────────────────────────────────────────


def _new_household(client, first="Fatou", email="fatou@example.test") -> dict:
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": first, "email": email, "password": "secret123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _invite(client, headers, email, role="adult"):
    r = client.post(f"{V1}/households/current/invitations", json={"email": email, "role": role}, headers=headers)
    assert r.status_code == 201, r.text


def test_existing_user_email_points_to_settings(client, outbox):
    fatou = _new_household(client)
    _invite(client, fatou, "koffi.k@email.com")
    assert "l’invitation vous attend dans Réglages" in outbox[-1][2]


def test_existing_user_accepts_and_moves_household(client, koffi, awa, outbox):
    fatou = _new_household(client)
    _invite(client, fatou, "koffi.k@email.com", role="teen")

    received = client.get(f"{V1}/invitations/received", headers=koffi).json()
    assert len(received) == 1
    inv = received[0]
    assert (inv["household_name"], inv["invited_by"], inv["role_label"]) == ("Foyer de Fatou", "Fatou", "ado")

    r = client.post(f"{V1}/invitations/{inv['id']}/accept", headers=koffi)
    assert r.status_code == 200, r.text
    assert r.json()["member"]["role"] == "teen"

    me = client.get(f"{V1}/auth/me", headers=koffi).json()  # same token, new household
    assert me["household_id"] == r.json()["household_id"] != "h-kouassi"
    kouassi = {m["first_name"] for m in client.get(f"{V1}/members", headers=awa).json()}
    assert "Koffi" not in kouassi
    assert "Koffi" in {m["first_name"] for m in client.get(f"{V1}/members", headers=fatou).json()}
    assert client.get(f"{V1}/invitations/received", headers=koffi).json() == []
    # Koffi's private account stays behind and is no longer visible to him.
    assert "a-koffi-perso" not in {a["id"] for a in client.get(f"{V1}/accounts", headers=koffi).json()}


def test_leaving_admin_hands_household_to_next_account(client, awa, koffi, outbox):
    fatou = _new_household(client)
    _invite(client, fatou, "awa.kouassi@email.com")
    inv = client.get(f"{V1}/invitations/received", headers=awa).json()[0]
    assert client.post(f"{V1}/invitations/{inv['id']}/accept", headers=awa).status_code == 200
    me = client.get(f"{V1}/auth/me", headers=koffi).json()
    assert me["member"]["role"] == "admin"


def test_decline_and_ownership(client, koffi, awa, outbox):
    fatou = _new_household(client)
    _invite(client, fatou, "koffi.k@email.com")
    inv = client.get(f"{V1}/invitations/received", headers=koffi).json()[0]

    assert client.post(f"{V1}/invitations/{inv['id']}/accept", headers=awa).status_code == 404  # not Awa's
    assert client.post(f"{V1}/invitations/{inv['id']}/decline", headers=koffi).status_code == 204
    assert client.get(f"{V1}/invitations/received", headers=koffi).json() == []
    pending = client.get(f"{V1}/households/current", headers=fatou).json()["invitations"]
    assert all(i["email"] != "koffi.k@email.com" for i in pending)
    # Inviting again after a refusal works.
    _invite(client, fatou, "koffi.k@email.com")
    assert len(client.get(f"{V1}/invitations/received", headers=koffi).json()) == 1
