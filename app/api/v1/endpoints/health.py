from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import Ctx, CtxDep
from app.application import premium
from app.core import clock
from app.core.exceptions import forbidden, not_found
from app.core.fr import appointment_label, hm, short_date
from app.core.security import new_id
from app.infrastructure.db.models import Appointment, IntakeLog, Medication, MedicationStock, ShoppingItem

router = APIRouter(prefix="/health", tags=["health"])


def intake_json(m: Medication, log: IntakeLog | None, day: date, now: datetime) -> dict:
    course = None
    if m.course_end:
        total = (m.course_end - m.course_start).days + 1 if m.course_start else None
        n = (day - m.course_start).days + 1 if m.course_start else None
        course = f"Jusqu’au {short_date(m.course_end)}" + (f" (J{n}/{total})" if total else "")
    elif log is None and day == now.date():
        h, mi = map(int, m.time.split(":"))
        mins = h * 60 + mi - (now.hour * 60 + now.minute)
        if 0 < mins <= 120:
            course = f"Rappel dans {max(1, round(mins / 60))} h" if mins >= 60 else f"Rappel dans {mins} min"
    return {
        "id": m.id,
        "member_id": m.member_id,
        "time": m.time,
        "product": m.product,
        "dose": m.dose,
        "taken": log is not None,
        "taken_note": f"Prise confirmée à {hm(log.taken_at)}" if log else None,
        "course_note": course,
    }


CONSENT_REQUIRED = "Acceptez d’abord l’utilisation de vos données de santé."


def _check(ctx: Ctx) -> None:
    if not ctx.sees_health_and_bills:
        raise forbidden()
    if ctx.user.health_consent_at is None:
        # 428: the app shows the consent screen, then retries.
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, CONSENT_REQUIRED)


@router.post("/consent")
def give_consent(ctx: CtxDep) -> dict:
    if not ctx.sees_health_and_bills:
        raise forbidden()
    ctx.user.health_consent_at = ctx.user.health_consent_at or clock.now()
    ctx.db.commit()
    return {"health_consent": True}


@router.delete("/consent")
def withdraw_consent(ctx: CtxDep, erase: bool = Query(default=False, description="also erase my own health data")) -> dict:
    """Health screens and medication reminders stop for this person. With `erase`, their own
    medications, intakes and appointments are deleted (not those of children they manage)."""
    ctx.user.health_consent_at = None
    if erase:
        mine = select(Medication.id).where(Medication.member_id == ctx.me.id)
        ctx.db.execute(delete(IntakeLog).where(IntakeLog.medication_id.in_(mine)))
        ctx.db.execute(delete(Medication).where(Medication.member_id == ctx.me.id))
        ctx.db.execute(delete(Appointment).where(Appointment.member_id == ctx.me.id))
    ctx.db.commit()
    return {"health_consent": False}


def _active(m: Medication, day: date) -> bool:
    return (m.course_start is None or m.course_start <= day) and (m.course_end is None or day <= m.course_end)


@router.get("")
def overview(ctx: CtxDep, day: date | None = Query(default=None, alias="date")) -> dict:
    """Screen 16."""
    _check(ctx)
    db, now = ctx.db, clock.now()
    day = day or now.date()
    meds = [m for m in db.scalars(select(Medication).where(Medication.household_id == ctx.hid)) if _active(m, day)]
    logs = {
        lg.medication_id: lg
        for lg in db.scalars(select(IntakeLog).where(IntakeLog.medication_id.in_([m.id for m in meds]), IntakeLog.date == day))
    }
    meds.sort(key=lambda m: m.time)
    stocks = db.scalars(
        select(MedicationStock).where(
            MedicationStock.household_id == ctx.hid, MedicationStock.remaining <= MedicationStock.alert_threshold
        )
    )
    appts = list(db.scalars(select(Appointment).where(Appointment.household_id == ctx.hid)))
    appts.sort(key=lambda a: (a.at is None, a.at or datetime.max))
    return {
        "intakes": [intake_json(m, logs.get(m.id), day, now) for m in meds],
        "stock_alerts": [
            {
                "id": s.id,
                "product": s.product,
                "remaining": s.remaining,
                "unit": s.unit,
                "in_shopping_list": shopping_has(db, ctx.hid, s.product, now.date()),
            }
            for s in stocks
        ],
        "appointments": [
            {
                "title": a.title,
                "member_id": a.member_id,
                "when": appointment_label(a.at) if a.at else (a.plan_label or "à planifier"),
                "days_left": (a.at.date() - day).days if a.at else None,
                "to_plan": a.at is None,
            }
            for a in appts
            if a.at is None or a.at.date() >= day
        ],
    }


@router.post("/intakes/{medication_id}/toggle")
def toggle(
    medication_id: str,
    ctx: CtxDep,
    day: date | None = Query(default=None, alias="date"),
    taken: bool | None = Query(default=None, description="explicit state (offline replays); flips when omitted"),
) -> dict:
    """Confirms (or cancels) an intake; confirming decrements the stock of that product."""
    _check(ctx)
    db, now = ctx.db, clock.now()
    day = day or now.date()
    m = db.get(Medication, medication_id)
    if m is None or m.household_id != ctx.hid:
        raise not_found("Prise")
    log = db.scalars(select(IntakeLog).where(IntakeLog.medication_id == m.id, IntakeLog.date == day)).first()
    stock = db.scalars(
        select(MedicationStock).where(MedicationStock.household_id == ctx.hid, MedicationStock.product == m.product)
    ).first()
    if taken is not None and taken == (log is not None):
        return intake_json(m, log, day, now)  # already in the requested state
    if log:
        db.delete(log)
        if stock:
            stock.remaining += 1
        log = None
    else:
        log = IntakeLog(id=new_id("il"), medication_id=m.id, date=day, taken_at=now)
        db.add(log)
        if stock and stock.remaining > 0:
            stock.remaining -= 1
            # Module link (Complet): stock falling to its alert level goes on this week's shopping list.
            if stock.remaining == stock.alert_threshold and premium.module_links_active(ctx.household):
                add_to_shopping(db, ctx.hid, stock, now.date())
    db.commit()
    return intake_json(m, log, day, now)


def shopping_has(db: Session, household_id: str, product: str, today: date) -> bool:
    week = today - timedelta(days=today.weekday())
    return (
        db.scalars(
            select(ShoppingItem.id).where(
                ShoppingItem.household_id == household_id,
                ShoppingItem.week_start == week,
                ShoppingItem.name == product,
                ~ShoppingItem.checked,
            )
        ).first()
        is not None
    )


def add_to_shopping(db: Session, household_id: str, stock: MedicationStock, today: date) -> bool:
    """Once per week and product (an item already there, not bought yet, is enough)."""
    if shopping_has(db, household_id, stock.product, today):
        return False
    db.add(
        ShoppingItem(
            id=new_id("si"), household_id=household_id, week_start=today - timedelta(days=today.weekday()),
            name=stock.product, quantity=f"1 {stock.unit}" if stock.unit else "1", price=0, place="supermarket",
            source="Stock santé bas", checked=False, auto=False,
        )
    )
    return True


@router.post("/stocks/{stock_id}/shopping")
def stock_to_shopping(stock_id: str, ctx: CtxDep) -> dict:
    """"Ajouter aux courses" on a low stock."""
    _check(ctx)
    s = ctx.db.get(MedicationStock, stock_id)
    if s is None or s.household_id != ctx.hid:
        raise not_found("Stock")
    added = add_to_shopping(ctx.db, ctx.hid, s, clock.today())
    ctx.db.commit()
    return {"added": added, "in_shopping_list": True}
