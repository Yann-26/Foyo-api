from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from app.api.deps import Ctx, CtxDep
from app.api.v1.endpoints.finances import create_operation
from app.application import premium
from app.core import clock
from app.core.exceptions import bad_request, forbidden, not_found
from app.core.fr import add_months, days_in_month, short_date, thousands
from app.core.security import new_id
from app.infrastructure.db.models import Account, Bill, Maintenance, Vehicle
from app.schemas.operations import OperationIn

router = APIRouter(prefix="/deadlines", tags=["deadlines"])


class PayIn(BaseModel):
    account_id: str | None = None


class BillIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    amount: int = Field(gt=0, le=100_000_000)
    due_date: date
    every_months: Literal[1, 2, 3, 6, 12] | None = None  # recurring: the next one is created when paid
    category_id: str = Field(default="factures", max_length=30)


class MileageIn(BaseModel):
    mileage: int = Field(ge=0, le=5_000_000)


class MaintenanceDoneIn(BaseModel):
    cost: int | None = Field(default=None, ge=0, le=100_000_000)  # asked by the app ("Combien ?")
    account_id: str | None = None
    mileage: int | None = Field(default=None, ge=0, le=5_000_000)


def bill_json(b: Bill, today: date) -> dict:
    paid = b.paid_at is not None
    return {
        "id": b.id,
        "label": b.label,
        "amount": b.amount,
        "due_date": b.due_date.isoformat(),
        "due_label": f"Payée le {short_date(b.paid_at)}" if paid else f"Échéance {short_date(b.due_date)}",
        "days_left": 0 if paid else (b.due_date - today).days,
        "paid": paid,
        "every_months": b.every_months,
    }


def maintenance_json(m: Maintenance, mileage: int, today: date) -> dict:
    candidates = []  # (urgency 0..1, progress, badge, urgent)
    if m.due_km is not None:
        left = m.due_km - mileage
        progress = 1 - left / m.interval_km if m.interval_km else 0.5
        urgent = left <= 1000
        candidates.append((progress, "Bientôt" if urgent else f"{thousands(max(left, 0))} km", urgent))
    if m.due_date is not None:
        left = (m.due_date - today).days
        progress = 1 - left / m.period_days if m.period_days else 0.5
        urgent = left <= 15
        candidates.append((progress, "Bientôt" if urgent else f"J-{max(left, 0)}", urgent))
    progress, badge, urgent = max(candidates, key=lambda c: c[0]) if candidates else (0.0, "", False)
    return {
        "id": m.id,
        "label": m.label,
        "detail": m.detail,
        "last_done": short_date(m.last_done_at) if m.last_done_at else None,
        "last_cost": m.last_cost,
        "progress": round(min(max(progress, 0.0), 1.0), 2),
        "badge": badge,
        "urgent": urgent,
    }


@router.get("")
def deadlines(ctx: CtxDep) -> dict:
    """Screen 17 — bills of the month and vehicle maintenance."""
    if not ctx.sees_health_and_bills:
        raise forbidden()
    today = clock.today()
    month_start = today.replace(day=1)
    bills = list(
        ctx.db.scalars(
            select(Bill)
            .where(Bill.household_id == ctx.hid, or_(Bill.paid_at.is_(None), Bill.paid_at >= month_start))
            .order_by(Bill.due_date)
        )
    )
    v = ctx.db.scalars(select(Vehicle).where(Vehicle.household_id == ctx.hid)).first()
    vehicle = None
    if v:
        items = ctx.db.scalars(select(Maintenance).where(Maintenance.vehicle_id == v.id).order_by(Maintenance.id))
        vehicle = {"id": v.id, "name": v.name, "mileage": v.mileage, "items": [maintenance_json(m, v.mileage, today) for m in items]}
    return {
        "fixed_budget": sum(b.amount for b in bills),
        "bills": [bill_json(b, today) for b in bills],
        "vehicle": vehicle,
    }


@router.post("/bills/{bill_id}/pay")
def pay_bill(bill_id: str, ctx: CtxDep, body: PayIn | None = None) -> dict:
    """Marks a bill paid. In the Complet plan it also records the expense (module link, spec F20)."""
    ctx.require_edit()
    b = ctx.db.get(Bill, bill_id)
    if b is None or b.household_id != ctx.hid:
        raise not_found("Facture")
    if b.paid_at is not None:
        raise bad_request("Facture déjà payée")
    b.paid_at = clock.today()
    if b.every_months:
        nxt = add_months(b.due_date, b.every_months)
        ctx.db.add(
            Bill(
                id=new_id("b"), household_id=ctx.hid, label=b.label, amount=b.amount, category_id=b.category_id,
                due_date=nxt.replace(day=min(b.due_date.day, days_in_month(nxt.year, nxt.month))),
                every_months=b.every_months,
            )
        )
    operation = None
    if premium.module_links_active(ctx.household):
        account_id = body.account_id if body and body.account_id else None
        if account_id is None:
            shared = ctx.db.scalars(
                select(Account).where(Account.household_id == ctx.hid, ~Account.is_private).order_by(Account.balance.desc())
            ).first()
            account_id = shared.id if shared else None
        if account_id:
            operation = create_operation(
                ctx,
                OperationIn(
                    type="expense", amount=b.amount, category_id=b.category_id, account_id=account_id,
                    label=b.label, origin="deadline",
                ),
            )
    ctx.db.commit()
    data = bill_json(b, clock.today())
    data["operation_id"] = operation.id if operation else None
    return data


def _bill(ctx: Ctx, bill_id: str) -> Bill:
    b = ctx.db.get(Bill, bill_id)
    if b is None or b.household_id != ctx.hid:
        raise not_found("Facture")
    return b


@router.post("/bills", status_code=status.HTTP_201_CREATED)
def add_bill(body: BillIn, ctx: CtxDep) -> dict:
    """A bill to remember (reminders at J-7, J-3, J-1 and on the day)."""
    if not ctx.sees_health_and_bills:
        raise forbidden()
    ctx.require_edit()
    b = Bill(
        id=new_id("b"), household_id=ctx.hid, label=body.label.strip(), amount=body.amount, due_date=body.due_date,
        category_id=body.category_id, every_months=body.every_months,
    )
    ctx.db.add(b)
    ctx.db.commit()
    return bill_json(b, clock.today())


@router.delete("/bills/{bill_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bill(bill_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    ctx.db.delete(_bill(ctx, bill_id))
    ctx.db.commit()


def _vehicle(ctx: Ctx, vehicle_id: str) -> Vehicle:
    if not ctx.sees_health_and_bills:
        raise forbidden()
    v = ctx.db.get(Vehicle, vehicle_id)
    if v is None or v.household_id != ctx.hid:
        raise not_found("Véhicule")
    return v


@router.put("/vehicles/{vehicle_id}/mileage")
def set_mileage(vehicle_id: str, body: MileageIn, ctx: CtxDep) -> dict:
    """Kilométrage relevé au compteur: maintenance due by distance follows."""
    v = _vehicle(ctx, vehicle_id)
    if body.mileage < v.mileage:
        raise bad_request(f"Le compteur affichait déjà {thousands(v.mileage)} km.")
    v.mileage = body.mileage
    ctx.db.commit()
    return deadlines(ctx)


@router.post("/maintenances/{maintenance_id}/done")
def maintenance_done(maintenance_id: str, body: MaintenanceDoneIn, ctx: CtxDep) -> dict:
    """"Entretien fait": the next one is scheduled from today / the mileage. With a cost, in the Complet
    plan, it is also recorded as a Transport expense (module link)."""
    ctx.require_edit()
    m = ctx.db.get(Maintenance, maintenance_id)
    if m is None:
        raise not_found("Entretien")
    v = _vehicle(ctx, m.vehicle_id)
    today = clock.today()
    if body.mileage is not None:
        if body.mileage < v.mileage:
            raise bad_request(f"Le compteur affichait déjà {thousands(v.mileage)} km.")
        v.mileage = body.mileage
    if m.interval_km:
        m.due_km = v.mileage + m.interval_km
    if m.period_days:
        m.due_date = today + timedelta(days=m.period_days)
    m.last_done_at = today
    m.last_cost = body.cost
    if body.cost and premium.module_links_active(ctx.household):
        account_id = body.account_id
        if account_id is None:
            shared = ctx.db.scalars(
                select(Account).where(Account.household_id == ctx.hid, ~Account.is_private).order_by(Account.balance.desc())
            ).first()
            account_id = shared.id if shared else None
        if account_id:
            create_operation(
                ctx,
                OperationIn(
                    type="expense", amount=body.cost, category_id="transport", account_id=account_id,
                    label=f"{m.label} · {v.name}", origin="deadline",
                ),
            )
    ctx.db.commit()
    return deadlines(ctx)
