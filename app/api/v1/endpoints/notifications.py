"""Devices that receive pushes, notification preferences, and a test push."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import DB, CtxDep, get_current_user
from app.application.notifications import notify_user
from app.application.upcoming import upcoming
from app.core import clock
from app.core.exceptions import bad_request
from app.core.security import new_id
from app.infrastructure.db.models import Device, User
from app.infrastructure.notifications.fcm_client import PushMessage

router = APIRouter(tags=["notifications"])

UserDep = Annotated[User, Depends(get_current_user)]


class DeviceIn(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    platform: Literal["android", "ios"]
    local_reminders: bool = False


class PrefsIn(BaseModel):
    push_enabled: bool | None = None
    quiet_hours: bool | None = None
    daily_report: bool | None = None


def _prefs(u: User) -> dict:
    return {"push_enabled": u.push_enabled, "quiet_hours": u.quiet_hours, "daily_report": u.daily_report}


@router.post("/devices", status_code=status.HTTP_204_NO_CONTENT)
def register_device(body: DeviceIn, db: DB, user: UserDep) -> None:
    """Called after sign-in and whenever Firebase rotates the token. A phone moves to the latest account."""
    now = clock.now()
    d = db.scalars(select(Device).where(Device.token == body.token)).first()
    if d is None:
        db.add(
            Device(
                id=new_id("dv"), user_id=user.id, token=body.token, platform=body.platform,
                local_reminders=body.local_reminders, created_at=now, last_seen_at=now,
            )
        )
    else:
        d.user_id, d.platform, d.last_seen_at = user.id, body.platform, now
        d.local_reminders = body.local_reminders
    db.commit()


@router.delete("/devices/{token}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_device(token: str, db: DB, user: UserDep) -> None:
    """Called on sign-out, so the phone stops receiving this account's reminders."""
    db.execute(delete(Device).where(Device.token == token, Device.user_id == user.id))
    db.commit()


@router.get("/reminders/upcoming")
def upcoming_reminders(ctx: CtxDep, days: int = Query(default=7, ge=1, le=14)) -> list[dict]:
    """Next reminders for this account, for the phone to schedule locally (they fire offline).
    Times are UTC instants; texts are ready to display."""
    return upcoming(ctx.db, ctx.user, ctx.me, days=days)


@router.get("/notifications/settings")
def get_prefs(user: UserDep) -> dict:
    return _prefs(user)


@router.patch("/notifications/settings")
def patch_prefs(body: PrefsIn, db: DB, user: UserDep) -> dict:
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(user, k, v)
    db.commit()
    return _prefs(user)


@router.post("/notifications/test")
def test_push(db: DB, user: UserDep) -> dict:
    """Sends a push to the caller's phones now (ignores quiet hours) — to check the Firebase setup."""
    if not db.scalars(select(Device.id).where(Device.user_id == user.id)).first():
        raise bad_request("Aucun appareil enregistré pour les notifications sur ce compte")
    if not user.push_enabled:
        raise bad_request("Les notifications push sont désactivées dans vos réglages")
    notify_user(
        db, user, PushMessage("Foyo", "Les notifications fonctionnent sur ce téléphone.", {"route": "/settings"}),
        kind="test",  # not muted by quiet hours, and sent even to phones that schedule reminders locally
    )
    return {"sent": True}
