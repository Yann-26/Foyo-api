"""Reminders a phone schedules on its own, so they fire even without a connection.

Same texts and rules as the server pushes (app/workers/reminder_jobs.py): the assignee's tasks at
time − offset, and medications for the member or the profiles they manage. Quiet hours and the push
switch are applied here, since the phone fires them without asking the server.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.notifications import in_quiet_hours
from app.application.tasks import rotation_pool
from app.core import clock
from app.domain.services.recurrence_service import is_due
from app.domain.services.rotation_service import rotation_assignee
from app.infrastructure.db.models import Household, IntakeLog, Medication, Member, Occurrence, Task, User
from app.workers.reminder_jobs import DEFAULT_TIME, medication_message, task_message

MAX_ITEMS = 60  # iOS keeps at most 64 pending local notifications per app


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def _represented(db: Session, me: Member) -> dict[str, Member]:
    """The member and the profiles without account they manage (a child's reminders go to the admin)."""
    managed = db.scalars(
        select(Member).where(Member.managed_by == me.id, Member.active, Member.user_id.is_(None))
    )
    return {me.id: me, **{m.id: m for m in managed}}


def upcoming(db: Session, user: User, me: Member, days: int = 7, now: datetime | None = None) -> list[dict]:
    if not user.push_enabled:
        return []
    now = now or clock.now()
    hh = db.get(Household, me.household_id)
    people = _represented(db, me)
    items: list[tuple[datetime, dict]] = []

    def add(at: datetime, key: str, msg, kind: str) -> None:  # noqa: ANN001
        if at <= now:
            return
        if kind != "medication" and user.quiet_hours and in_quiet_hours(at):
            return
        items.append(
            (
                at,
                {
                    "key": key,
                    "at": clock.to_utc(at).isoformat().replace("+00:00", "Z"),
                    "title": msg.title,
                    "body": msg.body,
                    "route": msg.data["route"],
                    "kind": kind,
                    # For the notification buttons (Fait / Pris) handled on the phone.
                    "task_id": msg.data.get("task_id"),
                    "medication_id": msg.data.get("medication_id"),
                },
            )
        )

    dates = [now.date() + timedelta(days=i) for i in range(days + 1)]

    if hh.plan in ("complete", "tasks"):
        tasks = list(db.scalars(select(Task).where(Task.household_id == hh.id, Task.active)))
        existing = {
            (o.task_id, o.date): o
            for o in db.scalars(
                select(Occurrence).where(
                    Occurrence.household_id == hh.id, Occurrence.date >= dates[0], Occurrence.date <= dates[-1]
                )
            )
        }
        pool = rotation_pool(db, hh.id) if any(t.rotation for t in tasks) else []
        for day in dates:
            for t in tasks:
                if not is_due(t.recurrence, t.start_date, day):
                    continue
                occ = existing.get((t.id, day))
                if occ is not None:
                    if occ.status != "todo":
                        continue  # done, postponed or missed: nothing to remind
                    assignee = occ.assignee_id
                else:
                    assignee = rotation_assignee(pool, t.start_date, day, t.assignee_id) if t.rotation else t.assignee_id
                if assignee not in people:
                    continue
                at = _at(day, t.time or DEFAULT_TIME) - timedelta(minutes=t.reminder_offset or 0)
                add(at, f"task:{t.id}:{day}", task_message(t, people[assignee]), "task")

    # No health consent: no medication reminders on the phone either.
    meds = (
        list(db.scalars(select(Medication).where(Medication.member_id.in_(people.keys()))))
        if user.health_consent_at
        else []
    )
    taken_today = set(
        db.scalars(select(IntakeLog.medication_id).where(IntakeLog.date == dates[0], IntakeLog.medication_id.in_([m.id for m in meds])))
    )
    for day in dates:
        for med in meds:
            if med.course_start and day < med.course_start or med.course_end and day > med.course_end:
                continue
            if day == dates[0] and med.id in taken_today:
                continue
            add(_at(day, med.time), f"med:{med.id}:{day}", medication_message(med, people[med.member_id]), "medication")

    items.sort(key=lambda x: x[0])
    return [item for _, item in items[:MAX_ITEMS]]
