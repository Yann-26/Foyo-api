"""Who gets a push, whether they want it now, and making sure it goes out only once."""

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import clock
from app.core.database import SessionLocal
from app.core.security import new_id
from app.infrastructure.db.models import Device, Member, NotificationLog, User
from app.infrastructure.notifications import fcm_client
from app.infrastructure.notifications.fcm_client import PushMessage

log = logging.getLogger("foyo.push")

# Kinds that ignore quiet hours (spec: "22:00 → 6:00, sauf médicaments").
URGENT_KINDS = {"medication", "test"}
# Kinds the phone can schedule itself (GET /reminders/upcoming): not pushed to such phones.
LOCAL_KINDS = {"task", "medication"}


def in_quiet_hours(now: datetime) -> bool:
    return now.hour >= 22 or now.hour < 6


def recipient_user(db: Session, member: Member | None) -> User | None:
    """The account that should hear about `member`: themselves, or the admin managing their profile."""
    if member is None or not member.active:
        return None
    if member.user_id:
        return db.get(User, member.user_id)
    if member.managed_by:
        return recipient_user(db, db.get(Member, member.managed_by))
    return None


def notify_user(
    db: Session,
    user: User | None,
    msg: PushMessage,
    *,
    kind: str,
    key: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Sends `msg` to every device of `user` unless their preferences say no. `key` de-duplicates."""
    if user is None or not user.push_enabled:
        return False
    now = now or clock.now()
    if kind == "report" and not user.daily_report:
        return False
    if user.quiet_hours and kind not in URGENT_KINDS and in_quiet_hours(now):
        return False
    q = select(Device.token).where(Device.user_id == user.id)
    if kind in LOCAL_KINDS:
        q = q.where(~Device.local_reminders)
    tokens = list(db.scalars(q))
    if not tokens:
        return False
    if key:
        db.add(NotificationLog(id=new_id("nl"), user_id=user.id, key=key, sent_at=now))
        try:
            db.commit()
        except IntegrityError:  # already sent (another run got there first)
            db.rollback()
            return False
    dead = fcm_client.send_push(tokens, PushMessage(msg.title, msg.body, {**msg.data, "kind": kind}))
    if dead:
        db.execute(delete(Device).where(Device.token.in_(dead)))
        db.commit()
    return True


def notify_member(
    db: Session,
    member_id: str | None,
    msg: PushMessage,
    *,
    kind: str,
    key: str | None = None,
    now: datetime | None = None,
) -> bool:
    member = db.get(Member, member_id) if member_id else None
    return notify_user(db, recipient_user(db, member), msg, kind=kind, key=key, now=now)


def notify_member_later(member_id: str | None, title: str, body: str, route: str, kind: str) -> None:
    """For FastAPI BackgroundTasks: runs after the response, with its own DB session."""
    try:
        with SessionLocal() as db:
            notify_member(db, member_id, PushMessage(title, body, {"route": route}), kind=kind)
    except Exception:
        log.exception("Push to member %s failed", member_id)


def notify_user_later(user_id: str, title: str, body: str, route: str, kind: str) -> None:
    try:
        with SessionLocal() as db:
            notify_user(db, db.get(User, user_id), PushMessage(title, body, {"route": route}), kind=kind)
    except Exception:
        log.exception("Push to user %s failed", user_id)
