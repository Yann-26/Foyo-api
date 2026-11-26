import csv
import io
from collections import defaultdict
from datetime import date, datetime, time
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import or_, select

from app.api.deps import Ctx, CtxDep
from app.application import finance_ops, premium
from app.core import clock
from app.core.exceptions import bad_request, forbidden, not_found
from app.core.fr import MONTHS_CHART, MONTHS_FULL, add_months, days_in_month
from app.core.security import new_id
from app.domain.services.balance_service import balance_deltas
from app.infrastructure.db.models import (
    Account,
    Budget,
    Debt,
    Member,
    Operation,
    RecurringOperation,
    SavingsGoal,
    Tontine,
)
from app.infrastructure.storage import files
from app.schemas.accounts import AccountIn
from app.schemas.budgets import BudgetIn
from app.schemas.operations import OperationIn, RecurringIn, RecurringPatchIn
from app.schemas.savings import DebtIn, DepositIn, GoalIn, RepayIn, TontineIn, TontinePayIn

router = APIRouter(tags=["finances"])

MonthQ = Query(default=None, pattern=r"^\d{4}-\d{2}$", description="YYYY-MM, current month by default")


def _month(month: str | None) -> tuple[int, int]:
    if month:
        y, m = month.split("-")
        return int(y), int(m)
    t = clock.today()
    return t.year, t.month


def _bounds(y: int, m: int) -> tuple[datetime, datetime]:
    start = datetime(y, m, 1)
    end = datetime.combine(date(y, m, days_in_month(y, m)), time.max)
    return start, end


def _visible_accounts(ctx: Ctx):
    """Private accounts are only returned to their owner (spec §5.10)."""
    return select(Account).where(
        Account.household_id == ctx.hid, or_(~Account.is_private, Account.owner_id == ctx.me.id)
    )


def account_json(a: Account) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "type": a.type,
        "balance": a.balance,
        "is_private": a.is_private,
        "owner_id": a.owner_id,
    }


def operation_json(o: Operation) -> dict:
    return {
        "id": o.id,
        "type": o.type,
        "amount": o.amount,
        "category_id": o.category_id,
        "account_id": o.account_id,
        "to_account_id": o.to_account_id,
        "date": o.date.isoformat(timespec="seconds"),
        "author_id": o.author_id,
        "label": o.label,
        "origin": o.origin,
        "parent_id": o.parent_id,
        "has_receipt": bool(o.receipt_key),
    }


def _ops(ctx: Ctx, y: int, m: int) -> list[Operation]:
    start, end = _bounds(y, m)
    visible = {a.id for a in ctx.db.scalars(_visible_accounts(ctx))}
    rows = ctx.db.scalars(
        select(Operation)
        .where(Operation.household_id == ctx.hid, Operation.date >= start, Operation.date <= end)
        .order_by(Operation.date.desc())
    )
    return [o for o in rows if o.account_id in visible]


def _totals(ops: list[Operation]) -> tuple[int, int]:
    income = sum(o.amount for o in ops if o.type == "income")
    expense = sum(o.amount for o in ops if o.type == "expense")
    return income, expense


def create_operation(ctx: Ctx, body: OperationIn) -> Operation:
    """Shared by POST /operations and the module links (bills, shopping list)."""
    when = body.date.replace(tzinfo=None) if body.date else clock.now()
    premium.check_limit(ctx.household, "operations_per_month", premium.operations_in_month(ctx.db, ctx.household, when))
    accounts = {a.id: a for a in ctx.db.scalars(_visible_accounts(ctx))}
    if body.account_id not in accounts:
        raise bad_request("Compte inconnu")
    if body.type == "transfer":
        if not body.to_account_id or body.to_account_id not in accounts:
            raise bad_request("Compte de destination inconnu")
        if body.to_account_id == body.account_id:
            raise bad_request("Choisissez deux comptes différents")
    author = body.author_id or ctx.me.id
    a = ctx.db.get(Member, author)
    if a is None or a.household_id != ctx.hid:
        author = ctx.me.id
    op = finance_ops.write_operation(
        ctx.db, ctx.hid, accounts, type=body.type, amount=body.amount, category_id=body.category_id,
        account_id=body.account_id, to_account_id=body.to_account_id, when=when, author_id=author,
        label=body.label, origin=body.origin, fee=body.fee,
    )
    if body.repeat_every_months:
        day = when.day
        nxt = finance_ops.due_date(when.year, when.month, day)
        r = RecurringOperation(
            id=new_id("ro"), household_id=ctx.hid, type=body.type, amount=body.amount, fee=body.fee,
            category_id=body.category_id, account_id=body.account_id,
            to_account_id=body.to_account_id if body.type == "transfer" else None, label=body.label,
            author_id=author, every_months=body.repeat_every_months, day=day, next_date=nxt, active=True,
        )
        r.next_date = finance_ops.after(r, nxt)
        ctx.db.add(r)
    return op


# ── Accounts ──────────────────────────────────────────────────────────────────


@router.get("/accounts")
def list_accounts(ctx: CtxDep) -> list[dict]:
    ctx.require_module("finances")
    return [account_json(a) for a in ctx.db.scalars(_visible_accounts(ctx).order_by(Account.id))]


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(body: AccountIn, ctx: CtxDep) -> dict:
    ctx.require_module("finances")
    premium.check_limit(ctx.household, "accounts", premium.accounts(ctx.db, ctx.household))
    if not body.is_private:
        ctx.require_edit()
    a = Account(
        id=new_id("a"),
        household_id=ctx.hid,
        name=body.name,
        type=body.type,
        balance=body.balance,
        is_private=body.is_private,
        owner_id=ctx.me.id if body.is_private else None,
    )
    ctx.db.add(a)
    ctx.db.commit()
    return account_json(a)


# ── Operations ────────────────────────────────────────────────────────────────


@router.get("/operations")
def list_operations(ctx: CtxDep, month: str | None = MonthQ) -> list[dict]:
    ctx.require_module("finances")
    return [operation_json(o) for o in _ops(ctx, *_month(month))]


@router.post("/operations", status_code=status.HTTP_201_CREATED)
def add_operation(body: OperationIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    """Screen 26. One call updates the account balance, the budget and the monthly totals."""
    ctx.require_module("finances")
    if ctx.me.role in ("child", "helper"):
        raise forbidden()
    op = create_operation(ctx, body)
    ctx.db.commit()
    for cat in {op.category_id, *(["frais"] if body.fee else [])}:
        if op.type == "expense" or cat == "frais":
            background.add_task(finance_ops.notify_budget_later, ctx.hid, cat, op.date)
    return operation_json(op)


@router.delete("/operations/{operation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_operation(operation_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    op = _get_op(ctx, operation_id)
    # The fee of a transfer goes with it.
    for o in [op, *ctx.db.scalars(select(Operation).where(Operation.parent_id == op.id))]:
        for acc_id, delta in balance_deltas(o.type, o.amount, o.account_id, o.to_account_id).items():
            ctx.db.get(Account, acc_id).balance -= delta
        files.delete(o.receipt_key)
        ctx.db.delete(o)
    ctx.db.commit()


def _get_op(ctx: Ctx, operation_id: str) -> Operation:
    op = ctx.db.get(Operation, operation_id)
    visible = {a.id for a in ctx.db.scalars(_visible_accounts(ctx))}
    if op is None or op.household_id != ctx.hid or op.account_id not in visible:
        raise not_found("Opération")
    return op


@router.post("/operations/{operation_id}/receipt")
async def upload_receipt(operation_id: str, ctx: CtxDep, file: UploadFile) -> dict:
    """Photo of the receipt (spec F16). Replaces the previous one."""
    ctx.require_module("finances")
    if ctx.me.role in ("child", "helper"):
        raise forbidden()
    op = _get_op(ctx, operation_id)
    try:
        key = files.save(f"receipts/{ctx.hid}", await file.read(files.MAX_BYTES + 1), file.content_type)
    except files.InvalidUpload as e:
        raise bad_request(str(e)) from e
    files.delete(op.receipt_key)
    op.receipt_key = key
    ctx.db.commit()
    return operation_json(op)


@router.get("/operations/{operation_id}/receipt")
def get_receipt(operation_id: str, ctx: CtxDep) -> FileResponse:
    ctx.require_module("finances")
    op = _get_op(ctx, operation_id)
    if not op.receipt_key or (path := files.path_of(op.receipt_key)) is None:
        raise not_found("Reçu")
    return FileResponse(path, media_type=files.media_type(op.receipt_key), headers={"Cache-Control": "private, max-age=86400"})


# ── Recurring operations ──────────────────────────────────────────────────────


def recurring_json(r: RecurringOperation) -> dict:
    return {
        "id": r.id, "type": r.type, "amount": r.amount, "fee": r.fee, "category_id": r.category_id,
        "account_id": r.account_id, "to_account_id": r.to_account_id, "label": r.label,
        "every_months": r.every_months, "day": r.day, "next_date": r.next_date.isoformat(), "active": r.active,
    }


def _get_recurring(ctx: Ctx, rid: str) -> RecurringOperation:
    r = ctx.db.get(RecurringOperation, rid)
    visible = {a.id for a in ctx.db.scalars(_visible_accounts(ctx))}
    if r is None or r.household_id != ctx.hid or r.account_id not in visible:
        raise not_found("Opération récurrente")
    return r


@router.get("/recurring-operations")
def list_recurring(ctx: CtxDep) -> list[dict]:
    ctx.require_module("finances")
    visible = {a.id for a in ctx.db.scalars(_visible_accounts(ctx))}
    rows = ctx.db.scalars(
        select(RecurringOperation).where(RecurringOperation.household_id == ctx.hid).order_by(RecurringOperation.next_date)
    )
    return [recurring_json(r) for r in rows if r.account_id in visible]


@router.post("/recurring-operations", status_code=status.HTTP_201_CREATED)
def create_recurring(body: RecurringIn, ctx: CtxDep) -> dict:
    ctx.require_module("finances")
    ctx.require_edit()
    accounts = {a.id for a in ctx.db.scalars(_visible_accounts(ctx))}
    if body.account_id not in accounts or (body.type == "transfer" and body.to_account_id not in accounts):
        raise bad_request("Compte inconnu")
    start = body.start or finance_ops.first_due(body.day, clock.today())
    r = RecurringOperation(
        id=new_id("ro"), household_id=ctx.hid, type=body.type, amount=body.amount, fee=body.fee,
        category_id=body.category_id, account_id=body.account_id,
        to_account_id=body.to_account_id if body.type == "transfer" else None, label=body.label,
        author_id=ctx.me.id, every_months=body.every_months, day=body.day, next_date=start, active=True,
    )
    ctx.db.add(r)
    ctx.db.commit()
    return recurring_json(r)


@router.patch("/recurring-operations/{rid}")
def patch_recurring(rid: str, body: RecurringPatchIn, ctx: CtxDep) -> dict:
    ctx.require_edit()
    r = _get_recurring(ctx, rid)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is not None:
            setattr(r, k, v)
    if "day" in data or data.get("active"):
        r.next_date = finance_ops.first_due(r.day, max(r.next_date, clock.today()) if "day" not in data else clock.today())
    ctx.db.commit()
    return recurring_json(r)


@router.delete("/recurring-operations/{rid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recurring(rid: str, ctx: CtxDep) -> None:
    """Stops future ones; operations already written stay."""
    ctx.require_edit()
    ctx.db.delete(_get_recurring(ctx, rid))
    ctx.db.commit()


# ── Export ────────────────────────────────────────────────────────────────────


@router.get("/operations/export.csv")
def export_csv(
    ctx: CtxDep,
    period: Literal["month", "quarter", "year"] = "month",
    month: str | None = MonthQ,
) -> Response:
    """Spreadsheet of the period's operations (Excel-friendly: `;`, UTF-8 with BOM)."""
    ctx.require_module("finances")
    ctx.require_export()
    y, m = _month(month)
    months = _period_months(y, m, period)
    if (period != "month" or (y, m) != (clock.today().year, clock.today().month)):
        premium.require_premium(ctx.household, "L’export des périodes passées et des trimestres / années")
    names = {a.id: a.name for a in ctx.db.scalars(_visible_accounts(ctx))}
    members = {x.id: x.first_name for x in ctx.db.scalars(select(Member).where(Member.household_id == ctx.hid))}
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Date", "Type", "Catégorie", "Montant (FCFA)", "Compte", "Vers", "Libellé", "Saisi par"])
    kinds = {"expense": "Dépense", "income": "Entrée", "transfer": "Transfert"}
    ops = sorted((o for yy, mm in months for o in _ops(ctx, yy, mm)), key=lambda o: o.date)
    for o in ops:
        signed = -o.amount if o.type == "expense" else o.amount
        w.writerow([
            o.date.strftime("%d/%m/%Y"), kinds.get(o.type, o.type), finance_ops.CATEGORY_NAMES.get(o.category_id, o.category_id),
            signed, names.get(o.account_id, ""), names.get(o.to_account_id or "", ""), o.label or "",
            members.get(o.author_id, ""),
        ])
    name = f"foyo-operations-{y:04d}-{m:02d}-{period}.csv"
    return Response(
        "\ufeff" + out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _period_months(y: int, m: int, period: str) -> list[tuple[int, int]]:
    """The months of the period containing (y, m): the month, its quarter, or its year."""
    if period == "year":
        return [(y, k) for k in range(1, 13)]
    if period == "quarter":
        first = (m - 1) // 3 * 3 + 1
        return [(y, k) for k in range(first, first + 3)]
    return [(y, m)]


# ── Budgets & summary ─────────────────────────────────────────────────────────


def budgets_for(ctx: Ctx, y: int, m: int) -> list[dict]:
    key = f"{y:04d}-{m:02d}"
    spent: dict[str, int] = defaultdict(int)
    for o in _ops(ctx, y, m):
        if o.type == "expense":
            spent[o.category_id] += o.amount
    rows = ctx.db.scalars(select(Budget).where(Budget.household_id == ctx.hid, Budget.month == key).order_by(Budget.id))
    return [{"category_id": b.category_id, "planned": b.planned, "spent": spent[b.category_id]} for b in rows]


@router.get("/budgets")
def list_budgets(ctx: CtxDep, month: str | None = MonthQ) -> list[dict]:
    ctx.require_module("finances")
    return budgets_for(ctx, *_month(month))


@router.put("/budgets")
def upsert_budget(body: BudgetIn, ctx: CtxDep) -> list[dict]:
    ctx.require_module("finances")
    ctx.require_edit()
    y, m = _month(body.month)
    key = f"{y:04d}-{m:02d}"
    b = ctx.db.scalars(
        select(Budget).where(Budget.household_id == ctx.hid, Budget.month == key, Budget.category_id == body.category_id)
    ).first()
    if b is None:
        premium.check_limit(ctx.household, "budgets", premium.budgets_in_month(ctx.db, ctx.household, key))
        b = Budget(id=new_id("b"), household_id=ctx.hid, month=key, category_id=body.category_id, planned=body.planned)
        ctx.db.add(b)
    b.planned = body.planned
    ctx.db.commit()
    return budgets_for(ctx, y, m)


@router.get("/finances/summary")
def summary(ctx: CtxDep, month: str | None = MonthQ) -> dict:
    """Screen 25 — household balance = sum of the *shared* accounts."""
    ctx.require_module("finances")
    y, m = _month(month)
    ops = _ops(ctx, y, m)
    income, expense = _totals(ops)
    accounts = list(ctx.db.scalars(_visible_accounts(ctx).order_by(Account.id)))
    budgets = budgets_for(ctx, y, m)
    today = clock.today()
    days_left = days_in_month(y, m) - today.day if (y, m) == (today.year, today.month) else 0
    return {
        "year": y,
        "month": m,
        "income": income,
        "expense": expense,
        "days_left": days_left,
        "household_balance": sum(a.balance for a in accounts if not a.is_private),
        "accounts": [account_json(a) for a in accounts],
        "budget_planned": sum(b["planned"] for b in budgets),
        "budget_spent": sum(b["spent"] for b in budgets),
        "recent": [operation_json(o) for o in ops[:4]],
    }


@router.get("/reports/finances")
def finance_report(
    ctx: CtxDep, month: str | None = MonthQ, period: Literal["month", "quarter", "year"] = "month"
) -> dict:
    """Screen 29. `period` = the month, its quarter or its year (Premium for quarter / year)."""
    ctx.require_module("finances")
    y, m = _month(month)
    today = clock.today()
    if period != "month":
        premium.require_premium(ctx.household, "Les rapports trimestriels et annuels")
    elif (y, m) != (today.year, today.month):
        premium.require_premium(ctx.household, "Les rapports des mois passés")
    months = _period_months(y, m, period)
    ops = [o for yy, mm in months for o in _ops(ctx, yy, mm)]
    income, expense = _totals(ops)
    by_cat: dict[str, int] = defaultdict(int)
    for o in ops:
        if o.type == "expense":
            by_cat[o.category_id] += o.amount
    labels, inc, exp = [], [], []
    # Bars: the last 6 months for a month, the period's months otherwise.
    chart = (
        [(d.year, d.month) for d in (add_months(date(y, m, 1), -back) for back in range(5, -1, -1))]
        if period == "month"
        else months
    )
    for yy, mm in chart:
        i, e = _totals(_ops(ctx, yy, mm))
        labels.append(MONTHS_CHART[mm - 1])
        inc.append(i)
        exp.append(e)
    days = sum(
        (today.day if (yy, mm) == (today.year, today.month) else days_in_month(yy, mm))
        for yy, mm in months
        if date(yy, mm, 1) <= today
    )
    return {
        "year": y,
        "month": m,
        "period": period,
        "period_label": _period_label(y, m, period),
        "income": income,
        "expense": expense,
        "days": max(1, days),
        "by_category": [{"category_id": c, "amount": a} for c, a in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "months": labels,
        "monthly_income": inc,
        "monthly_expense": exp,
    }


def _period_label(y: int, m: int, period: str) -> str:
    if period == "year":
        return f"Année {y}"
    if period == "quarter":
        q = (m - 1) // 3 + 1
        return f"{q}{'er' if q == 1 else 'e'} trimestre {y}"
    return f"{MONTHS_FULL[m - 1].capitalize()} {y}"


# ── Savings, tontines, debts (F19, screen 30) ─────────────────────────────────


def tontine_json(t: Tontine, today: date) -> dict:
    months = (today.year - t.start_date.year) * 12 + today.month - t.start_date.month
    current = min(t.member_count, max(1, months + 1))
    payout = add_months(t.start_date, t.my_turn - 1)
    pay_day = min(t.pay_day, days_in_month(today.year, today.month))
    nxt = date(today.year, today.month, pay_day)
    if nxt < today:
        nxt = add_months(date(today.year, today.month, 1), 1)
        nxt = nxt.replace(day=min(t.pay_day, days_in_month(nxt.year, nxt.month)))
    return {
        "id": t.id,
        "name": t.name,
        "contribution": t.contribution,
        "period": "mois",
        "member_count": t.member_count,
        "current_turn": current,
        "my_turn": t.my_turn,
        "payout_label": f"{MONTHS_FULL[payout.month - 1]} {payout.year}",
        "next_payment_label": f"{nxt.day} {MONTHS_FULL[nxt.month - 1]}",
    }


@router.get("/savings")
def savings(ctx: CtxDep) -> dict:
    premium.require_premium(ctx.household, "Épargne, tontines et dettes")
    ctx.require_module("finances")
    db, today = ctx.db, clock.today()
    return {
        "goals": [
            {"id": g.id, "name": g.name, "target": g.target, "saved": g.saved, "note": g.note}
            for g in db.scalars(select(SavingsGoal).where(SavingsGoal.household_id == ctx.hid).order_by(SavingsGoal.id))
        ],
        "tontines": [
            tontine_json(t, today)
            for t in db.scalars(select(Tontine).where(Tontine.household_id == ctx.hid).order_by(Tontine.id))
        ],
        "debts": [
            debt_json(d) for d in db.scalars(select(Debt).where(Debt.household_id == ctx.hid).order_by(Debt.id))
        ],
    }


@router.post("/savings/goals", status_code=status.HTTP_201_CREATED)
def create_goal(body: GoalIn, ctx: CtxDep) -> dict:
    premium.require_premium(ctx.household, "Les objectifs d’épargne")
    ctx.require_edit()
    g = SavingsGoal(id=new_id("g"), household_id=ctx.hid, **body.model_dump())
    ctx.db.add(g)
    ctx.db.commit()
    return {"id": g.id, "name": g.name, "target": g.target, "saved": g.saved, "note": g.note}


@router.post("/savings/goals/{goal_id}/deposit")
def deposit(goal_id: str, body: DepositIn, ctx: CtxDep) -> dict:
    premium.require_premium(ctx.household, "Les objectifs d’épargne")
    g = ctx.db.get(SavingsGoal, goal_id)
    if g is None or g.household_id != ctx.hid:
        raise not_found("Objectif")
    g.saved += body.amount
    ctx.db.commit()
    return {"id": g.id, "name": g.name, "target": g.target, "saved": g.saved, "note": g.note}


@router.post("/savings/debts", status_code=status.HTTP_201_CREATED)
def create_debt(body: DebtIn, ctx: CtxDep) -> dict:
    premium.require_premium(ctx.household, "Les prêts et dettes")
    ctx.require_edit()
    d = Debt(id=new_id("d"), household_id=ctx.hid, repaid=0, **body.model_dump())
    ctx.db.add(d)
    ctx.db.commit()
    return debt_json(d)


def debt_json(d: Debt) -> dict:
    return {
        "id": d.id, "direction": d.direction, "person": d.person, "amount": d.amount, "note": d.note,
        "repaid": d.repaid, "remaining": max(0, d.amount - d.repaid),
    }


@router.post("/savings/debts/{debt_id}/repay")
def repay(debt_id: str, body: RepayIn, ctx: CtxDep) -> dict:
    """Partial or full repayment. With an account, it is also written: money coming back (lent) is an
    income, money paid back (borrowed) an expense."""
    premium.require_premium(ctx.household, "Les prêts et dettes")
    ctx.require_edit()
    d = ctx.db.get(Debt, debt_id)
    if d is None or d.household_id != ctx.hid:
        raise not_found("Dette")
    left = d.amount - d.repaid
    if left <= 0:
        raise bad_request("Déjà remboursée")
    if body.amount > left:
        raise bad_request(f"Il ne reste que {left} F à rembourser")
    if body.account_id:
        lent = d.direction == "lent"
        create_operation(
            ctx,
            OperationIn(
                type="income" if lent else "expense", amount=body.amount,
                category_id="autre_entree" if lent else "dettes", account_id=body.account_id,
                label=f"Remboursement {'de' if lent else 'à'} {d.person}", origin="debt",
            ),
        )
    d.repaid += body.amount
    ctx.db.commit()
    return debt_json(d)


@router.delete("/savings/debts/{debt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_debt(debt_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    d = ctx.db.get(Debt, debt_id)
    if d is None or d.household_id != ctx.hid:
        raise not_found("Dette")
    ctx.db.delete(d)
    ctx.db.commit()


@router.post("/savings/tontines", status_code=status.HTTP_201_CREATED)
def create_tontine(body: TontineIn, ctx: CtxDep) -> dict:
    premium.require_premium(ctx.household, "Les tontines")
    ctx.require_edit()
    if body.my_turn > body.member_count:
        raise bad_request("Votre tour dépasse le nombre de membres")
    t = Tontine(id=new_id("tn"), household_id=ctx.hid, **{**body.model_dump(), "start_date": body.start_date.replace(day=1)})
    ctx.db.add(t)
    ctx.db.commit()
    return tontine_json(t, clock.today())


@router.post("/savings/tontines/{tontine_id}/pay", status_code=status.HTTP_201_CREATED)
def pay_tontine(tontine_id: str, body: TontinePayIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    """This month's contribution, as a "Tontine" expense."""
    premium.require_premium(ctx.household, "Les tontines")
    t = ctx.db.get(Tontine, tontine_id)
    if t is None or t.household_id != ctx.hid:
        raise not_found("Tontine")
    body_op = OperationIn(
        type="expense", amount=t.contribution, category_id="tontine", account_id=body.account_id,
        label=f"Cotisation {t.name}", origin="tontine",
    )
    return add_operation(body_op, ctx, background)
