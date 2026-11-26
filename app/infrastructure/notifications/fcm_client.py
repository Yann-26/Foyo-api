"""Push delivery.

PUSH_BACKEND=log  writes each notification to the server log (development).
PUSH_BACKEND=fcm  sends it with the Firebase Cloud Messaging HTTP v1 API, authenticated with the
                  service account in FCM_CREDENTIALS_FILE (Firebase console → Project settings →
                  Service accounts → Generate new private key).
"""

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import Settings, get_settings

log = logging.getLogger("foyo.push")

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
ANDROID_CHANNEL = "foyo_reminders"


class PushConfigError(RuntimeError):
    pass


@dataclass
class PushMessage:
    title: str
    body: str
    # Strings only (FCM requirement). `route` tells the app which screen to open on tap.
    data: dict[str, str] = field(default_factory=dict)


def check_config(s: Settings | None = None) -> None:
    s = s or get_settings()
    if s.push_backend == "log":
        return
    if s.push_backend != "fcm":
        raise PushConfigError(f"Unknown PUSH_BACKEND {s.push_backend!r} (use 'log' or 'fcm')")
    if not s.fcm_credentials_file:
        raise PushConfigError("PUSH_BACKEND=fcm needs FCM_CREDENTIALS_FILE (Firebase service-account JSON)")
    try:
        with open(s.fcm_credentials_file) as f:
            if not json.load(f).get("project_id"):
                raise PushConfigError("FCM_CREDENTIALS_FILE has no project_id")
    except OSError as e:
        raise PushConfigError(f"Cannot read FCM_CREDENTIALS_FILE: {e}") from e


@lru_cache
def _fcm_session(path: str):  # noqa: ANN202
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(path, scopes=[FCM_SCOPE])
    return AuthorizedSession(creds), creds.project_id


def fcm_payload(token: str, msg: PushMessage) -> dict:
    return {
        "message": {
            "token": token,
            "notification": {"title": msg.title, "body": msg.body},
            "data": msg.data,
            "android": {"priority": "high", "notification": {"channel_id": ANDROID_CHANNEL}},
            "apns": {"payload": {"aps": {"sound": "default"}}},
        }
    }


def _is_dead_token(status: int, body: str) -> bool:
    """FCM says the app was uninstalled or the token is malformed: forget it."""
    return status == 404 or (status == 400 and "registration token" in body.lower()) or "UNREGISTERED" in body


def send_push(tokens: list[str], msg: PushMessage) -> list[str]:
    """Sends to every token; returns the tokens FCM rejected for good (to delete)."""
    if not tokens:
        return []
    s = get_settings()
    if s.push_backend == "log":
        log.warning("PUSH (not delivered, PUSH_BACKEND=log) to %d device(s): %s — %s %s", len(tokens), msg.title, msg.body, msg.data)
        return []
    check_config(s)
    session, project = _fcm_session(s.fcm_credentials_file)
    url = f"https://fcm.googleapis.com/v1/projects/{project}/messages:send"
    dead = []
    for token in tokens:
        try:
            r = session.post(url, json=fcm_payload(token, msg), timeout=10)
        except Exception:
            log.exception("FCM request failed")
            continue
        if r.status_code == 200:
            continue
        if _is_dead_token(r.status_code, r.text):
            dead.append(token)
        else:
            log.error("FCM refused a message (%s): %s", r.status_code, r.text[:300])
    return dead
