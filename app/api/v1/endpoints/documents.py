"""Data for the printable documents (spec §5.7). The PDF itself is drawn on the phone, offline;
this only returns what to draw: the week's tasks, the week's menu, the month's dates."""

from datetime import date, timedelta

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CtxDep
from app.application import premium
from app.application.tasks import rotation_pool
from app.core import clock
from app.core.fr import MONTHS_FULL, MONTHS_SHORT, days_in_month, short_date, thousands
from app.domain.services.recurrence_service import is_due
from app.domain.services.rotation_service import rotation_assignee
from app.infrastructure.db.models import Appointment, Bill, Dish, MealPlan, Member, Occurrence, Task

router = APIRouter(prefix="/documents", tags=["documents"])


def _week_label(start: date) -> str:
    end = start + timedelta(days=6)
    left = f"{start.day}" if start.month == end.month else short_date(start)
    return f"Semaine du {left} au {end.day} {MONTHS_FULL[end.month - 1]} {end.year}"


@router.get("/data")
def document_data(
    ctx: CtxDep,
    week: date | None = Query(default=None, description="any day of the week, current week by default"),
    month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$"),
) -> dict:
    ctx.require_export()
    db, hid = ctx.db, ctx.hid
    today = clock.today()
    start = (week or today) - timedelta(days=(week or today).weekday())
    y, m = map(int, month.split("-")) if month else (today.year, today.month)
    month_days = [date(y, m, d) for d in range(1, days_in_month(y, m) + 1)]

    members = list(db.scalars(select(Member).where(Member.household_id == hid, Member.active).order_by(Member.id)))
    tasks = list(db.scalars(select(Task).where(Task.household_id == hid, Task.active).order_by(Task.time, Task.name)))
    pool = rotation_pool(db, hid) if any(t.rotation for t in tasks) else []
    week_days = [start + timedelta(days=i) for i in range(7)]
    existing = {
        (o.task_id, o.date): o.assignee_id
        for o in db.scalars(
            select(Occurrence).where(Occurrence.household_id == hid, Occurrence.date >= week_days[0], Occurrence.date <= week_days[-1])
        )
    }

    def assignee(t: Task, day: date) -> str | None:
        if (t.id, day) in existing:
            return existing[(t.id, day)]
        return rotation_assignee(pool, t.start_date, day, t.assignee_id) if t.rotation else t.assignee_id

    def mine(assignee_id: str | None) -> bool:
        return ctx.sees_all_tasks or assignee_id == ctx.me.id

    week_entries = [
        {
            "date": day.isoformat(),
            "task_id": t.id,
            "name": t.name,
            "time": t.time,
            "category_id": t.category_id,
            "assignee_id": assignee(t, day),
        }
        for day in week_days
        for t in tasks
        if is_due(t.recurrence, t.start_date, day) and mine(assignee(t, day))
    ]

    meals = db.execute(
        select(MealPlan, Dish)
        .join(Dish, Dish.id == MealPlan.dish_id)
        .where(MealPlan.household_id == hid, MealPlan.date >= week_days[0], MealPlan.date <= week_days[-1])
        .order_by(MealPlan.date, MealPlan.time)
    ).all()

    month_items: dict[date, list[dict]] = {d: [] for d in month_days}
    for b in db.scalars(select(Bill).where(Bill.household_id == hid, Bill.due_date >= month_days[0], Bill.due_date <= month_days[-1])):
        month_items[b.due_date].append(
            {"kind": "bill", "label": f"{b.label} · {thousands(b.amount)} F", "done": b.paid_at is not None}
        )
    if ctx.sees_health_and_bills:
        for a in db.scalars(select(Appointment).where(Appointment.household_id == hid, Appointment.at.is_not(None))):
            if a.at.date() in month_items:
                month_items[a.at.date()].append({"kind": "appointment", "label": f"{a.at.hour}:{a.at.minute:02d} {a.title}", "done": False})
    else:  # bills and health are hidden from teens, children and helpers (spec §5.2)
        month_items = {d: [] for d in month_days}

    return {
        "household_name": ctx.household.name,
        "is_premium": premium.premium_active(ctx.household),
        "generated_at": clock.now().isoformat(timespec="minutes"),
        "members": [
            {"id": mb.id, "first_name": mb.first_name, "role": mb.role} for mb in members if ctx.sees_all_tasks or mb.id == ctx.me.id
        ],
        "week": {
            "start": start.isoformat(),
            "label": _week_label(start),
            "entries": week_entries,
            "menu": [
                {
                    "date": mp.date.isoformat(),
                    "slot": mp.slot,
                    "time": mp.time,
                    "dish_name": dish.name,
                    "cook_id": mp.cook_id,
                }
                for mp, dish in meals
                if mine(mp.cook_id)
            ],
        },
        "month": {
            "label": f"{MONTHS_FULL[m - 1].capitalize()} {y}",
            "short_label": f"{MONTHS_SHORT[m - 1]} {y}",
            "days": [
                {
                    "date": d.isoformat(),
                    "task_count": sum(
                        1
                        for t in tasks
                        if is_due(t.recurrence, t.start_date, d) and (ctx.sees_all_tasks or t.assignee_id == ctx.me.id)
                    ),
                    "items": month_items[d],
                }
                for d in month_days
            ],
        },
    }
