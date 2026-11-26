"""Personal data: export (right of access) and account deletion (right to erasure)."""

import shutil
from datetime import date, datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.application.membership import current_member, leave_current
from app.core import clock
from app.infrastructure.db.models import (
    Account,
    Appointment,
    Device,
    Household,
    IntakeLog,
    Medication,
    Member,
    Occurrence,
    Operation,
    Task,
    User,
)
from app.infrastructure.storage import files

FORMER_MEMBER = "Ancien membre"


def _iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v else None


def export_user(db: Session, user: User) -> dict:
    """Everything stored about this person, readable as JSON. Shared household data (other members'
    tasks, the household's accounts) is not personal data of the requester and stays out."""
    member = current_member(db, user)
    mids = [m.id for m in db.scalars(select(Member).where(Member.user_id == user.id))]
    data: dict = {
        "exported_at": clock.now().isoformat(timespec="seconds"),
        "account": {
            "first_name": user.first_name,
            "email": user.email,
            "email_verified": user.email_verified,
            "created_at": _iso(user.created_at),
            "google_linked": user.google_sub is not None,
            "apple_linked": user.apple_sub is not None,
            "health_consent_at": _iso(user.health_consent_at),
            "notifications": {
                "push": user.push_enabled,
                "quiet_hours": user.quiet_hours,
                "daily_report": user.daily_report,
            },
        },
        "devices": [
            {"platform": d.platform, "registered_at": _iso(d.created_at), "last_seen_at": _iso(d.last_seen_at)}
            for d in db.scalars(select(Device).where(Device.user_id == user.id))
        ],
        "membership": None,
        "tasks_assigned": [],
        "tasks_done": [],
        "private_accounts": [],
        "operations_entered": [],
        "medications": [],
        "appointments": [],
    }
    if member is None:
        return data
    hh = db.get(Household, member.household_id)
    data["membership"] = {
        "household": hh.name,
        "role": member.role,
        "points": member.points,
        "color": member.color,
    }
    data["tasks_assigned"] = [
        {"name": t.name, "category": t.category_id, "time": t.time, "recurrence": t.recurrence, "minutes": t.estimated_minutes}
        for t in db.scalars(select(Task).where(Task.assignee_id == member.id, Task.active))
    ]
    done = db.execute(
        select(Occurrence, Task)
        .join(Task, Task.id == Occurrence.task_id)
        .where(Occurrence.assignee_id.in_(mids), Occurrence.status == "done")
        .order_by(Occurrence.date)
    ).all()
    data["tasks_done"] = [
        {
            "date": _iso(o.date),
            "task": t.name,
            "minutes": round((o.actual_seconds or 0) / 60),
            "estimated": o.duration_estimated,
            "note": o.note,
            "photo": bool(o.photo_key),
        }
        for o, t in done
    ]
    names = {a.id: a.name for a in db.scalars(select(Account).where(Account.household_id == hh.id))}
    data["private_accounts"] = [
        {"name": a.name, "type": a.type, "balance": a.balance}
        for a in db.scalars(select(Account).where(Account.owner_id.in_(mids), Account.is_private))
    ]
    data["operations_entered"] = [
        {
            "date": _iso(op.date),
            "type": op.type,
            "amount": op.amount,
            "category": op.category_id,
            "account": names.get(op.account_id),
            "label": op.label,
        }
        for op in db.scalars(select(Operation).where(Operation.author_id.in_(mids)).order_by(Operation.date))
    ]
    meds = list(db.scalars(select(Medication).where(Medication.member_id.in_(mids))))
    taken = {}
    for log in db.scalars(select(IntakeLog).where(IntakeLog.medication_id.in_([m.id for m in meds]))):
        taken.setdefault(log.medication_id, []).append(_iso(log.taken_at))
    data["medications"] = [
        {
            "product": m.product,
            "dose": m.dose,
            "time": m.time,
            "course_start": _iso(m.course_start),
            "course_end": _iso(m.course_end),
            "taken": sorted(taken.get(m.id, [])),
        }
        for m in meds
    ]
    data["appointments"] = [
        {"title": a.title, "at": _iso(a.at), "plan": a.plan_label}
        for a in db.scalars(select(Appointment).where(Appointment.member_id.in_(mids)))
    ]
    return data


def delete_user(db: Session, user: User) -> str:
    """Erases the account. Alone in the household (no other member with an account): the whole household
    goes. Otherwise the person leaves it: the admin role passes on (as when leaving), their profile in the
    shared history becomes "Ancien membre", and their health data and private accounts are erased.
    Returns "household" or "member" (what was deleted)."""
    member = current_member(db, user)
    mids = [m.id for m in db.scalars(select(Member).where(Member.user_id == user.id))]
    outcome = "member"
    if member is not None:
        others = db.scalars(
            select(Member.id).where(
                Member.household_id == member.household_id,
                Member.active,
                Member.user_id.is_not(None),
                Member.id != member.id,
            )
        ).first()
        if others is None:
            hid = member.household_id
            db.execute(delete(Household).where(Household.id == hid))  # everything else cascades
            # Photos live outside the database: task proofs and receipts go with the household.
            for folder in ("tasks", "receipts"):
                shutil.rmtree(files.root() / folder / hid, ignore_errors=True)
            outcome = "household"
        else:
            leave_current(db, user)
    if mids:  # memberships left (other households included)
        meds = select(Medication.id).where(Medication.member_id.in_(mids))
        db.execute(delete(IntakeLog).where(IntakeLog.medication_id.in_(meds)))
        db.execute(delete(Medication).where(Medication.member_id.in_(mids)))
        db.execute(delete(Appointment).where(Appointment.member_id.in_(mids)))
        private = select(Account.id).where(Account.owner_id.in_(mids), Account.is_private)
        on_private = or_(Operation.account_id.in_(private), Operation.to_account_id.in_(private))
        for key in db.scalars(select(Operation.receipt_key).where(on_private, Operation.receipt_key.is_not(None))):
            files.delete(key)  # receipt photos of the private accounts
        db.execute(
            delete(Operation).where(or_(Operation.account_id.in_(private), Operation.to_account_id.in_(private)))
        )
        db.execute(delete(Account).where(Account.owner_id.in_(mids), Account.is_private))
        for m in db.scalars(select(Member).where(Member.id.in_(mids))):
            m.first_name, m.email, m.user_id, m.active = FORMER_MEMBER, None, None, False
    db.execute(delete(User).where(User.id == user.id))  # devices, logs, reset codes cascade
    db.commit()
    return outcome
