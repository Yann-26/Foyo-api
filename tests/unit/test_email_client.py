import logging
import smtplib

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.infrastructure.email import email_client


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what the client does."""

    instances: list["FakeSMTP"] = []
    refuse_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if FakeSMTP.refuse_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    def send_message(self, msg):
        self.calls.append("send")
        self.sent.append(msg)


def _settings(**kw) -> Settings:
    base = dict(
        email_backend="smtp",
        smtp_username="sender@example.test",
        smtp_password=SecretStr("app-password-16ch"),
        environment="test",
        _env_file=None,
    )
    return Settings(**{**base, **kw})


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.refuse_login = [], False
    monkeypatch.setattr(email_client.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_client.smtplib, "SMTP_SSL", FakeSMTP)


def _use(monkeypatch, s: Settings) -> None:
    monkeypatch.setattr(email_client, "get_settings", lambda: s)


def test_gmail_defaults_use_starttls_on_587(monkeypatch):
    _use(monkeypatch, _settings())
    email_client.send_email("user@example.test", "Code", "Votre code : 123456")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.calls == ["starttls", ("login", "sender@example.test", "app-password-16ch"), "send", "quit"]
    msg = smtp.sent[0]
    assert msg["To"] == "user@example.test"
    assert msg["From"] == "Foyo <sender@example.test>"
    assert "123456" in msg.get_content()


def test_port_465_uses_implicit_tls(monkeypatch):
    _use(monkeypatch, _settings(smtp_port=465, smtp_from="alias@example.test"))
    email_client.send_email("user@example.test", "Code", "x")
    smtp = FakeSMTP.instances[0]
    assert "starttls" not in smtp.calls
    assert smtp.sent[0]["From"] == "Foyo <alias@example.test>"


def test_missing_credentials_are_reported():
    with pytest.raises(email_client.EmailConfigError):
        email_client.check_config(_settings(smtp_password=SecretStr("")))
    email_client.check_config(_settings(email_backend="log", smtp_username=""))  # log needs nothing


def test_deliver_logs_failures_without_raising_or_leaking_the_password(monkeypatch, caplog):
    _use(monkeypatch, _settings())
    FakeSMTP.refuse_login = True
    with caplog.at_level(logging.ERROR, logger="foyo.email"):
        email_client.deliver("user@example.test", "Code", "x")
    assert "App Password" in caplog.text
    assert "app-password-16ch" not in caplog.text
