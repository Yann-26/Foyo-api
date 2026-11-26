import re
from datetime import timedelta

import pytest

from app.core import clock
from app.core.database import SessionLocal
from app.infrastructure.db.models import PasswordReset
from app.infrastructure.email import email_client

V1 = "/api/v1"
EMAIL = "awa.kouassi@email.com"


@pytest.fixture()
def outbox(monkeypatch):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(email_client, "send_email", lambda to, subject, body: sent.append((to, body)))
    return sent


def _code(outbox) -> str:
    return re.search(r"\b(\d{6})\b", outbox[-1][1]).group(1)


def test_unknown_email_gets_same_answer_and_no_mail(client, outbox):
    r = client.post(f"{V1}/auth/password/forgot", json={"email": "nobody@example.test"})
    assert r.status_code == 202
    assert outbox == []


def test_full_reset_flow_signs_out_other_devices(client, awa, outbox):
    r = client.post(f"{V1}/auth/password/forgot", json={"email": EMAIL})
    assert r.status_code == 202 and outbox[-1][0] == EMAIL
    code = _code(outbox)
    wrong = "000000" if code != "000000" else "111111"

    r = client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": wrong, "new_password": "nouveau123"})
    assert r.status_code == 400 and "4 essai" in r.json()["detail"]

    r = client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": code, "new_password": "nouveau123"})
    assert r.status_code == 200, r.text
    new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    assert client.get(f"{V1}/auth/me", headers=awa).status_code == 401  # old session revoked
    assert client.get(f"{V1}/auth/me", headers=new_headers).status_code == 200
    assert client.post(f"{V1}/auth/sign-in", json={"email": EMAIL, "password": "foyo1234"}).status_code == 401
    assert client.post(f"{V1}/auth/sign-in", json={"email": EMAIL, "password": "nouveau123"}).status_code == 200
    # A code works only once.
    r = client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": code, "new_password": "autre1234"})
    assert r.status_code == 400


def test_code_locks_after_five_wrong_attempts(client, outbox):
    client.post(f"{V1}/auth/password/forgot", json={"email": EMAIL})
    code = _code(outbox)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": wrong, "new_password": "nouveau123"})
    r = client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": code, "new_password": "nouveau123"})
    assert r.status_code == 400


def test_expired_code_is_refused(client, outbox):
    client.post(f"{V1}/auth/password/forgot", json={"email": EMAIL})
    code = _code(outbox)
    with SessionLocal() as db:
        for r in db.query(PasswordReset).all():
            r.expires_at = clock.now() - timedelta(minutes=1)
        db.commit()
    r = client.post(f"{V1}/auth/password/reset", json={"email": EMAIL, "code": code, "new_password": "nouveau123"})
    assert r.status_code == 400


def test_resend_is_throttled(client, outbox):
    client.post(f"{V1}/auth/password/forgot", json={"email": EMAIL})
    client.post(f"{V1}/auth/password/forgot", json={"email": EMAIL})
    assert len(outbox) == 1
