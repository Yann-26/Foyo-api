import re

import pytest

from app.api.v1.endpoints import auth as auth_endpoints
from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.auth.social import SocialIdentity, SocialNotConfigured
from app.infrastructure.db.models import Household, User
from app.infrastructure.email import email_client

V1 = "/api/v1"


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(email_client, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def _signup(client, email, code=None, first="Fatou"):
    r = client.post(
        f"{V1}/auth/sign-up",
        json={"first_name": first, "email": email, "password": "secret123", "invite_code": code},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, r.json()


# ── E-mail verification ──


def test_sign_up_sends_a_verification_link_that_works(client, outbox):
    h, _ = _signup(client, "fatou@example.test")
    to, subject, body = outbox[-1]
    assert to == "fatou@example.test" and "confirmez" in subject
    link = re.search(r"(http\S+verify-email\?token=\S+)", body).group(1)
    assert client.get(f"{V1}/auth/me", headers=h).json()["user"]["email_verified"] is False
    page = client.get(link.split("8000", 1)[1])
    assert page.status_code == 200 and "Adresse confirmée" in page.text
    assert client.get(f"{V1}/auth/me", headers=h).json()["user"]["email_verified"] is True
    assert client.post(f"{V1}/auth/verify-email/resend", headers=h).json()["detail"] == "Adresse déjà confirmée"


def test_bad_or_foreign_tokens_are_refused(client, awa, outbox):
    assert client.get(f"{V1}/auth/verify-email", params={"token": "nope"}).status_code == 400
    from app.core.security import create_purpose_token

    wrong_purpose = create_purpose_token("u-awa", "reset", "awa.kouassi@email.com")
    assert client.get(f"{V1}/auth/verify-email", params={"token": wrong_purpose}).status_code == 400
    old_address = create_purpose_token("u-awa", "verify", "old@example.test")
    assert client.get(f"{V1}/auth/verify-email", params={"token": old_address}).status_code == 400


# ── Google / Apple ──


@pytest.fixture()
def provider(monkeypatch):
    who: dict = {}

    def fake(p, token):
        if who.get("off"):
            raise SocialNotConfigured("Connexion Google non configurée sur ce serveur")
        return SocialIdentity(provider=p, **who["id"])

    monkeypatch.setattr(auth_endpoints, "verify_identity", fake)
    return who


def test_social_creates_an_account_with_its_household(client, provider):
    provider["id"] = {"sub": "g-123", "email": "new@gmail.test", "email_verified": True, "first_name": "Mariam"}
    r = client.post(f"{V1}/auth/social", json={"provider": "google", "id_token": "x" * 40})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = client.get(f"{V1}/auth/me", headers=h).json()
    assert (me["user"]["first_name"], me["user"]["email_verified"], me["member"]["role"]) == ("Mariam", True, "admin")
    # Next time: same account, found by the Google id even if the e-mail changed at Google.
    provider["id"] = {"sub": "g-123", "email": "renamed@gmail.test", "email_verified": True, "first_name": "Mariam"}
    again = client.post(f"{V1}/auth/social", json={"provider": "google", "id_token": "x" * 40}).json()
    assert again["user"]["id"] == r.json()["user"]["id"]


def test_social_links_an_existing_verified_address(client, provider):
    provider["id"] = {"sub": "apple-awa", "email": "awa.kouassi@email.com", "email_verified": True, "first_name": None}
    r = client.post(f"{V1}/auth/social", json={"provider": "apple", "id_token": "x" * 40})
    assert r.json()["user"]["id"] == "m-awa"  # Awa's existing account, not a new one
    with SessionLocal() as db:
        assert db.get(User, "u-awa").apple_sub == "apple-awa"


def test_social_never_takes_over_with_an_unverified_address(client, provider):
    provider["id"] = {"sub": "g-evil", "email": "awa.kouassi@email.com", "email_verified": False, "first_name": "X"}
    r = client.post(f"{V1}/auth/social", json={"provider": "google", "id_token": "x" * 40})
    assert r.status_code == 409


def test_social_not_configured(client, provider):
    provider["off"] = True
    r = client.post(f"{V1}/auth/social", json={"provider": "google", "id_token": "x" * 40})
    assert r.status_code == 503 and "non configurée" in r.json()["detail"]


# ── Invite link / code ──


def test_invite_code_join_renew_revoke(client, awa, koffi):
    hh = client.get(f"{V1}/households/current", headers=awa).json()
    code = hh["invite_code"]
    assert hh["invite_expires_in_days"] >= 6 and hh["invite_link"].endswith(code)
    assert client.get(f"{V1}/invitations/code/{code.lower()}").json()["household_name"] == "Famille Kouassi"

    # A new person signs up with the code: joins as the link's role.
    h, data = _signup(client, "cousine@example.test", code=code, first="Cousine")
    assert data["household_id"] == "h-kouassi"
    assert client.get(f"{V1}/auth/me", headers=h).json()["member"]["role"] == "adult"

    # Renewed for teens: the old code stops working.
    renewed = client.post(f"{V1}/households/current/invite-link", json={"role": "teen"}, headers=awa).json()
    assert renewed["invite_code"] != code and renewed["invite_role"] == "teen"
    assert client.get(f"{V1}/invitations/code/{code}").status_code == 404
    r = client.post(f"{V1}/auth/sign-up", json={"first_name": "X", "email": "x@example.test", "password": "secret123", "invite_code": code})
    assert r.status_code == 400

    # Only the admin manages the link.
    assert client.post(f"{V1}/households/current/invite-link", json={"role": "adult"}, headers=koffi).status_code == 403

    # Revoked: no code at all.
    revoked = client.delete(f"{V1}/households/current/invite-link", headers=awa).json()
    assert revoked["invite_code"] is None and revoked["invite_link"] is None
    assert client.get(f"{V1}/invitations/code/{renewed['invite_code']}").status_code == 404


def test_existing_account_joins_with_a_code(client, awa):
    fatou, _ = _signup(client, "fatou@example.test")
    code = client.get(f"{V1}/households/current", headers=awa).json()["invite_code"]
    r = client.post(f"{V1}/invitations/join", json={"code": code}, headers=fatou)
    assert r.status_code == 200 and r.json()["household_name"] == "Famille Kouassi"
    assert client.post(f"{V1}/invitations/join", json={"code": code}, headers=fatou).status_code == 400  # already in


def test_expired_code_is_refused(client, awa):
    code = client.get(f"{V1}/households/current", headers=awa).json()["invite_code"]
    with SessionLocal() as db:
        hh = db.get(Household, "h-kouassi")
        hh.invite_code_expires_at = clock.now().replace(year=2020)
        db.commit()
    assert client.get(f"{V1}/invitations/code/{code}").status_code == 404


# ── Assignment e-mail ──


def test_assigned_task_is_emailed(client, awa, outbox):
    client.post(
        f"{V1}/tasks",
        json={"name": "Laver la voiture", "category_id": "vehicule", "estimated_minutes": 30, "time": "10:00",
              "assignee_id": "m-koffi", "recurrence": {"kind": "weekly", "weekdays": [6]}},
        headers=awa,
    )
    to, subject, body = outbox[-1]
    assert to == "koffi.k@email.com" and subject == "Foyo — nouvelle tâche : Laver la voiture"
    assert "Awa vous a confié « Laver la voiture » (sam. · 10:00)" in body
    n = len(outbox)
    client.post(  # Inès has no account: no e-mail
        f"{V1}/tasks",
        json={"name": "Ranger", "category_id": "menage", "estimated_minutes": 5, "assignee_id": "m-ines"},
        headers=awa,
    )
    assert len(outbox) == n
