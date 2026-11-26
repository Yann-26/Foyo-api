"""Task occurrences: generation, serialization and stats shared by tasks and reports endpoints."""

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.fr import hm
from app.core.security import new_id
from app.domain.services.recurrence_service import is_due
from app.domain.services.rotation_service import ROTATION_ROLES, rotation_assignee
from app.infrastructure.db.models import Member, Occurrence, Task

POINTS_PER_TASK = 10


def rotation_pool(db: Session, household_id: str) -> list[str]:
    rows = db.scalars(
        select(Member)
        .where(Member.household_id == household_id, Member.active, Member.role.in_(ROTATION_ROLES))
        .order_by(Member.id)
    )
    return [m.id for m in rows]


def ensure_occurrences(db: Session, household_id: str, day: date) -> dict[str, Occurrence]:
    """Creates the missing occurrences of `day` and returns them keyed by task id."""
    tasks = db.scalars(select(Task).where(Task.household_id == household_id, Task.active)).all()
    existing = {
        o.task_id: o
        for o in db.scalars(select(Occurrence).where(Occurrence.household_id == household_id, Occurrence.date == day))
    }
    pool = None
    created = False
    for t in tasks:
        if t.id in existing or not is_due(t.recurrence, t.start_date, day):
            continue
        assignee = t.assignee_id
        if t.rotation:
            pool = pool if pool is not None else rotation_pool(db, household_id)
            assignee = rotation_assignee(pool, t.start_date, day, t.assignee_id)
        occ = Occurrence(
            id=new_id("oc"),
            task_id=t.id,
            household_id=household_id,
            date=day,
            assignee_id=assignee,
            status="todo",
            sub_steps_done=[False] * len(t.sub_steps or []),
        )
        db.add(occ)
        existing[t.id] = occ
        created = True
    if created:
        db.commit()
    return existing


def task_json(t: Task, occ: Occurrence | None = None, now: datetime | None = None) -> dict:
    now = now or clock.now()
    done_flags = (occ.sub_steps_done if occ else None) or []
    data = {
        "id": t.id,
        "name": t.name,
        "category_id": t.category_id,
        "recurrence": {
            "kind": t.recurrence.get("kind", "daily"),
            "weekdays": t.recurrence.get("weekdays") or [],
            "interval": t.recurrence.get("interval") or 1,
        },
        "estimated_minutes": t.estimated_minutes,
        "assignee_id": (occ.assignee_id if occ else None) or t.assignee_id,
        "time": t.time,
        "status": occ.status if occ else "todo",
        "rotation": t.rotation,
        "reminder_offset": t.reminder_offset,
        "detail": t.detail,
        "sub_steps": [
            {"label": label, "done": bool(done_flags[i]) if i < len(done_flags) else False}
            for i, label in enumerate(t.sub_steps or [])
        ],
        "occurrence_id": occ.id if occ else None,
        "elapsed_seconds": 0,
    }
    if occ and occ.status == "running" and occ.started_at:
        data["started_at"] = hm(occ.started_at)
        data["elapsed_seconds"] = max(0, int((now - occ.started_at).total_seconds()))
        data["detail"] = data["detail"] or f"Démarrée à {hm(occ.started_at)}"
    if occ and (occ.note or occ.photo_key):
        data["note"] = occ.note
        data["has_photo"] = bool(occ.photo_key)
    if occ and occ.status == "done" and occ.actual_seconds is not None:
        data["actual_minutes"] = round(occ.actual_seconds / 60)
        data["duration_estimated"] = occ.duration_estimated
    return data


def visible_tasks_filter(me: Member):
    """Helpers (aide à domicile) only see their own tasks (spec §5.2)."""
    return me.role == "helper"


def day_occurrences(db: Session, household_id: str, start: date, end: date) -> list[tuple[Occurrence, Task]]:
    rows = db.execute(
        select(Occurrence, Task)
        .join(Task, Task.id == Occurrence.task_id)
        .where(Occurrence.household_id == household_id, Occurrence.date >= start, Occurrence.date <= end)
    ).all()
    return [(o, t) for o, t in rows]


def minutes_of(o: Occurrence, t: Task) -> int:
    if o.actual_seconds is not None:
        return round(o.actual_seconds / 60)
    return t.estimated_minutes


def streak(db: Session, household_id: str, member_id: str | None, today: date, lookback: int = 400) -> tuple[int, int]:
    """(current streak, record) in days with at least one completed task (by `member_id`, or anyone)."""
    q = select(Occurrence.date).where(
        Occurrence.household_id == household_id,
        Occurrence.status == "done",
        Occurrence.date >= today - timedelta(days=lookback),
    )
    if member_id:
        q = q.where(Occurrence.assignee_id == member_id)
    days = set(db.scalars(q))
    cur = 0
    d = today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    prev = None
    for d in sorted(days):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        best = max(best, run)
        prev = d
    return cur, max(best, cur)


def member_load(db: Session, household_id: str, members: list[Member], start: date, end: date) -> list[dict]:
    minutes: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for o, t in day_occurrences(db, household_id, start, end):
        if o.status == "done" and o.assignee_id:
            minutes[o.assignee_id] += minutes_of(o, t)
            counts[o.assignee_id] += 1
    return [{"member_id": m.id, "minutes": minutes[m.id], "task_count": counts[m.id]} for m in members]
