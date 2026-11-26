import json

import pytest

from app.core.config import Settings
from app.infrastructure.notifications import fcm_client
from app.infrastructure.notifications.fcm_client import PushMessage


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None):  # noqa: A002
        self.calls.append((url, json))
        return self.responses.pop(0)


def _fcm(monkeypatch, tmp_path, session):
    creds = tmp_path / "sa.json"
    creds.write_text(json.dumps({"project_id": "foyo-test"}))
    s = Settings(environment="test", push_backend="fcm", fcm_credentials_file=str(creds), _env_file=None)
    monkeypatch.setattr(fcm_client, "get_settings", lambda: s)
    monkeypatch.setattr(fcm_client, "_fcm_session", lambda path: (session, "foyo-test"))


def test_sends_v1_payload_and_reports_dead_tokens(monkeypatch, tmp_path):
    session = FakeSession([FakeResponse(200), FakeResponse(404, "UNREGISTERED"), FakeResponse(500, "oops")])
    _fcm(monkeypatch, tmp_path, session)
    dead = fcm_client.send_push(["t1", "t2", "t3"], PushMessage("Titre", "Corps", {"route": "/today"}))
    assert dead == ["t2"]  # a server error is not a reason to drop a token
    url, body = session.calls[0]
    assert url == "https://fcm.googleapis.com/v1/projects/foyo-test/messages:send"
    assert body["message"]["token"] == "t1"
    assert body["message"]["notification"] == {"title": "Titre", "body": "Corps"}
    assert body["message"]["data"] == {"route": "/today"}
    assert body["message"]["android"]["notification"]["channel_id"] == "foyo_reminders"


def test_config_errors(tmp_path):
    with pytest.raises(fcm_client.PushConfigError, match="FCM_CREDENTIALS_FILE"):
        fcm_client.check_config(Settings(environment="test", push_backend="fcm", _env_file=None))
    with pytest.raises(fcm_client.PushConfigError, match="Cannot read"):
        fcm_client.check_config(Settings(environment="test", push_backend="fcm", fcm_credentials_file="/nope.json", _env_file=None))
    fcm_client.check_config(Settings(environment="test", _env_file=None))  # log backend needs nothing
