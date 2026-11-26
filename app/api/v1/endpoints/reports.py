from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import CtxDep
from app.application import premium
from app.application.tasks import day_occurrences, ensure_occurrences, member_load, minutes_of, streak
from app.core import clock
from app.core.fr import DAY_INITIALS, MONTHS_FULL, MONTHS_SHORT
from app.infrastructure.db.models import Member

router = APIRouter(prefix="/reports", tags=["reports"])

CATEGORY_LABELS = {
    "cuisine": "la cuisine", "vaisselle": "la vaisselle", "menage": "le ménage", "lessive": "la lessive",
    "repassage": "le repassage", "courses": "les courses", "sdb": "la salle de bain", "poubelles": "les poubelles",
    "reparations": "les réparations", "factures": "les factures", "vehicule": "le véhicule", "sante": "la santé",
    "medicaments": "les médicaments", "enfants": "les enfants",
}


def _members(ctx: CtxDep) -> list[Member]:
    return list(ctx.db.scalars(select(Member).where(Member.household_id == ctx.hid, Member.active).order_by(Member.id)))


def _own(ctx: CtxDep, rows: list) -> list:
    """Teens, children and helpers see their own figures only (spec §5.2)."""
    return [(o, t) for o, t in rows if o.assignee_id == ctx.me.id] if ctx.own_stats_only else rows


@router.get("/daily")
def daily(ctx: CtxDep, day: date | None = Query(default=None, alias="date")) -> dict:
    """Screen 13 — the 21:00 report."""
    day = day or clock.today()
    if day == clock.today():
        ensure_occurrences(ctx.db, ctx.hid, day)
    occs = _own(ctx, day_occurrences(ctx.db, ctx.hid, day, day))
    done = [(o, t) for o, t in occs if o.status == "done"]
    by_cat: dict[str, int] = defaultdict(int)
    by_member: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for o, t in done:
        mins = minutes_of(o, t)
        by_cat[t.category_id] += mins
        if o.assignee_id:
            by_member[o.assignee_id][0] += mins
            by_member[o.assignee_id][1] += 1
    total = sum(by_cat.values())
    insight = None
    if total and by_member:
        top_id, (top_min, _) = max(by_member.items(), key=lambda kv: kv[1][0])
        share = round(100 * top_min / total)
        if share >= 35 and len(by_member) > 1:
            name = ctx.db.get(Member, top_id).first_name
            cats = defaultdict(int)
            for o, t in done:
                if o.assignee_id == top_id:
                    cats[t.category_id] += minutes_of(o, t)
            top_cat = max(cats, key=cats.get)
            insight = (
                f"{name} porte {share} % du temps du foyer aujourd’hui. "
                f"Proposer une rotation sur {CATEGORY_LABELS.get(top_cat, top_cat)} ?"
            )
    money = None
    if ctx.household.plan == "complete" and ctx.me.role in ("admin", "adult"):
        from app.application import finance_ops

        money = finance_ops.overspent_line(finance_ops.overspent(ctx.db, ctx.hid, datetime.combine(day, time(12))))
    return {
        "date": day.isoformat(),
        "budget_alert": money,
        "done": len(done),
        "planned": len(occs),
        "total_minutes": total,
        "postponed": sum(o.status == "postponed" for o, _ in occs),
        "by_category": [
            {"category_id": c, "minutes": m} for c, m in sorted(by_cat.items(), key=lambda kv: -kv[1])
        ],
        "by_member": [
            {"member_id": mid, "minutes": v[0], "task_count": v[1]}
            for mid, v in sorted(by_member.items(), key=lambda kv: -kv[1][0])
        ],
        "insight": insight,
    }


def _range(period: str, today: date) -> tuple[date, date, str]:
    match period:
        case "day":
            return today, today, f"{today.day} {MONTHS_SHORT[today.month - 1]}"
        case "month":
            start = today.replace(day=1)
            return start, today, f"{MONTHS_FULL[today.month - 1].capitalize()} {today.year}"
        case "year":
            return today.replace(month=1, day=1), today, str(today.year)
    start = today - timedelta(days=6)
    left = f"{start.day}" if start.month == today.month else f"{start.day} {MONTHS_SHORT[start.month - 1]}"
    return start, today, f"{left} → {today.day} {MONTHS_SHORT[today.month - 1]}"


def _rate(occs) -> float:
    return sum(o.status == "done" for o, _ in occs) / len(occs) if occs else 0.0


@router.get("/tasks")
def task_stats(ctx: CtxDep, period: Literal["day", "week", "month", "year"] = "week") -> dict:
    """Screen 14."""
    premium.require_premium(ctx.household, "Les statistiques semaine, mois et année")
    today = clock.today()
    ensure_occurrences(ctx.db, ctx.hid, today)
    start, end, label = _range(period, today)
    occs = _own(ctx, day_occurrences(ctx.db, ctx.hid, start, end))
    span = (end - start).days + 1
    prev = _own(ctx, day_occurrences(ctx.db, ctx.hid, start - timedelta(days=span), start - timedelta(days=1)))
    rate = _rate(occs)

    done = [(o, t) for o, t in occs if o.status == "done"]
    total = sum(minutes_of(o, t) for o, t in done)
    if period in ("week", "day"):
        days = [today - timedelta(days=6 - i) for i in range(7)]
        per_day = {d: 0 for d in days}
        for o, t in _own(ctx, day_occurrences(ctx.db, ctx.hid, days[0], today)):
            if o.status == "done":
                per_day[o.date] += minutes_of(o, t)
        labels = [DAY_INITIALS[d.weekday()] for d in days]
        hours = [round(per_day[d] / 60, 1) for d in days]
    else:
        # Average hours per weekday over the period.
        sums, counts = [0] * 7, [0] * 7
        d = start
        while d <= end:
            counts[d.weekday()] += 1
            d += timedelta(days=1)
        for o, t in done:
            sums[o.date.weekday()] += minutes_of(o, t)
        labels = DAY_INITIALS
        hours = [round(sums[i] / counts[i] / 60, 1) if counts[i] else 0.0 for i in range(7)]

    by_member: dict[str, int] = defaultdict(int)
    for o, t in done:
        if o.assignee_id:
            by_member[o.assignee_id] += minutes_of(o, t)
    cur, record = streak(ctx.db, ctx.hid, ctx.me.id if ctx.own_stats_only else None, today)
    return {
        "period": period,
        "completion_rate": round(rate, 2),
        "completion_delta_pts": round((rate - _rate(prev)) * 100) if prev else 0,
        "total_minutes": total,
        "period_label": label,
        "day_labels": labels,
        "hours_per_day": hours,
        "member_share": {
            mid: round(m / total, 2) for mid, m in sorted(by_member.items(), key=lambda kv: -kv[1])
        } if total else {},
        "streak_days": cur,
        "record_days": record,
    }


@router.get("/load")
def load(ctx: CtxDep, period: Literal["week", "month"] = "week") -> list[dict]:
    """Screen 09 — weekly load per member, to assign fairly."""
    today = clock.today()
    days = 6 if period == "week" else 29
    members = _members(ctx)
    if ctx.own_stats_only:
        members = [m for m in members if m.id == ctx.me.id]
    return member_load(ctx.db, ctx.hid, members, today - timedelta(days=days), today)
