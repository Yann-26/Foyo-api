"""Writing operations to the accounts, shared by the API and the worker: transfer fees, recurring
operations, budget alerts (80 % and over budget)."""

from datetime import date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.application import premium
from app.core.fr import add_months, days_in_month, thousands
from app.core.security import new_id
from app.domain.services.balance_service import balance_deltas
from app.infrastructure.db.models import Account, Budget, Household, Member, Operation, RecurringOperation

FEE_CATEGORY = "frais"
ALERT_THRESHOLDS = (80, 100)


def write_operation(
    db: Session,
    household_id: str,
    accounts: dict[str, Account],
    *,
    type: str,
    amount: int,
    category_id: str,
    account_id: str,
    to_account_id: str | None,
    when: datetime,
    author_id: str,
    label: str | None,
    origin: str,
    fee: int = 0,
) -> Operation:
    """Adds the operation (and its fee, as a separate expense on the same account) and moves the balances.
    `accounts` holds the accounts the caller may use, by id. Nothing is committed."""
    op = Operation(
        id=new_id("o"),
        household_id=household_id,
        type=type,
        amount=amount,
        category_id=category_id,
        account_id=account_id,
        to_account_id=to_account_id if type == "transfer" else None,
        date=when,
        author_id=author_id,
        label=label,
        origin=origin,
    )
    ops = [op]
    if fee > 0:
        what = {"transfer": "transfert", "expense": "paiement", "income": "retrait"}[type]
        ops.append(
            Operation(
                id=new_id("o"),
                household_id=household_id,
                type="expense",
                amount=fee,
                category_id=FEE_CATEGORY,
                account_id=account_id,
                date=when,
                author_id=author_id,
                label=f"Frais de {what}" + (f" · {label}" if label else ""),
                origin="fee",
                parent_id=op.id,
            )
        )
    for o in ops:
        for acc_id, delta in balance_deltas(o.type, o.amount, o.account_id, o.to_account_id).items():
            accounts[acc_id].balance += delta
        db.add(o)
    return op


def due_date(year: int, month: int, day: int) -> date:
    """Day 31 (or 29, 30) means the last day in shorter months."""
    return date(year, month, min(day, days_in_month(year, month)))


def first_due(day: int, today: date) -> date:
    d = due_date(today.year, today.month, day)
    if d < today:
        nxt = add_months(date(today.year, today.month, 1), 1)
        d = due_date(nxt.year, nxt.month, day)
    return d


def after(r: RecurringOperation, d: date) -> date:
    nxt = add_months(date(d.year, d.month, 1), r.every_months)
    return due_date(nxt.year, nxt.month, r.day)


def run_recurring(db: Session, today: date) -> list[tuple[RecurringOperation, Operation]]:
    """Writes every recurring operation due up to today (catching up if the worker was down).
    Skipped, not lost, while the free plan's monthly limit is reached or an account is gone."""
    done = []
    for r in db.scalars(
        select(RecurringOperation).where(RecurringOperation.active, RecurringOperation.next_date <= today)
    ):
        hh = db.get(Household, r.household_id)
        accounts = {a.id: a for a in db.scalars(select(Account).where(Account.household_id == r.household_id))}
        for _ in range(24):  # at most two years of catching up
            if r.next_date > today:
                break
            if r.account_id not in accounts or (r.type == "transfer" and r.to_account_id not in accounts):
                r.active = False
                break
            when = datetime.combine(r.next_date, time(8, 0))
            if not premium.premium_active(hh):
                used = premium.operations_in_month(db, hh, when)
                if used >= premium.FREE_LIMITS["operations_per_month"]:
                    break
            op = write_operation(
                db, r.household_id, accounts, type=r.type, amount=r.amount, category_id=r.category_id,
                account_id=r.account_id, to_account_id=r.to_account_id, when=when, author_id=r.author_id,
                label=r.label, origin="recurring", fee=r.fee,
            )
            done.append((r, op))
            r.next_date = after(r, r.next_date)
        db.commit()
    return done


def budget_state(db: Session, household_id: str, category_id: str, when: datetime) -> tuple[int, int] | None:
    """(planned, spent) for the category's budget of that month, on shared accounts; None without budget."""
    key = f"{when.year:04d}-{when.month:02d}"
    b = db.scalars(
        select(Budget).where(Budget.household_id == household_id, Budget.month == key, Budget.category_id == category_id)
    ).first()
    if b is None or b.planned <= 0:
        return None
    start = datetime(when.year, when.month, 1)
    end = datetime.combine(date(when.year, when.month, days_in_month(when.year, when.month)), time.max)
    shared = select(Account.id).where(Account.household_id == household_id, ~Account.is_private)
    spent = db.scalar(
        select(func.coalesce(func.sum(Operation.amount), 0)).where(
            Operation.household_id == household_id,
            Operation.type == "expense",
            Operation.category_id == category_id,
            Operation.date >= start,
            Operation.date <= end,
            Operation.account_id.in_(shared),
        )
    )
    return b.planned, int(spent or 0)


def budget_alert(planned: int, spent: int, category_name: str) -> tuple[int, str, str] | None:
    """The highest threshold reached: (threshold, title, body)."""
    pct = spent * 100 // planned
    if pct >= 100:
        over = spent - planned
        return 100, f"Budget {category_name} dépassé", f"{thousands(spent)} F dépensés sur {thousands(planned)} F (+{thousands(over)} F)."
    if pct >= 80:
        return 80, f"Budget {category_name} à {pct} %", f"Il reste {thousands(planned - spent)} F sur {thousands(planned)} F ce mois-ci."
    return None


def alert_recipients(db: Session, household_id: str) -> list[str]:
    """Adults who manage the money: admins and adults."""
    return list(
        db.scalars(
            select(Member.id).where(
                Member.household_id == household_id, Member.active, Member.role.in_(("admin", "adult"))
            )
        )
    )


CATEGORY_NAMES = {
    "alimentation": "Alimentation", "transport": "Transport", "factures": "Factures", "scolarite": "Scolarité",
    "sante": "Santé", "loisirs": "Loisirs", "famille": "Famille", "tontine": "Tontine", "logement": "Logement",
    "habillement": "Habillement", "communication": "Communication", "frais": "Frais", "dettes": "Dettes",
    "autre": "Autre",
}


def notify_budget(db: Session, household_id: str, category_id: str, when: datetime, now: datetime | None = None) -> int:
    """After an expense: "Budget … à 80 %" then "… dépassé", each once per month and category."""
    from app.application.notifications import notify_member
    from app.infrastructure.notifications.fcm_client import PushMessage

    state = budget_state(db, household_id, category_id, when)
    alert = budget_alert(*state, CATEGORY_NAMES.get(category_id, category_id)) if state else None
    if alert is None:
        return 0
    threshold, title, body = alert
    key = f"budget:{household_id}:{when.year:04d}-{when.month:02d}:{category_id}:{threshold}"
    msg = PushMessage(title, body, {"route": "/finances/budgets"})
    return sum(
        notify_member(db, mid, msg, kind="budget", key=key, now=now) for mid in alert_recipients(db, household_id)
    )


def notify_budget_later(household_id: str, category_id: str, when: datetime) -> None:
    """For FastAPI BackgroundTasks (own session, after the response)."""
    import logging

    from app.core.database import SessionLocal

    try:
        with SessionLocal() as db:
            notify_budget(db, household_id, category_id, when)
    except Exception:
        logging.getLogger("foyo").exception("Budget alert failed")


def overspent(db: Session, household_id: str, when: datetime) -> list[tuple[str, int, int]]:
    """Budgets of the month already over: (category_id, planned, spent), the biggest overrun first."""
    key = f"{when.year:04d}-{when.month:02d}"
    rows = []
    for b in db.scalars(select(Budget).where(Budget.household_id == household_id, Budget.month == key)):
        state = budget_state(db, household_id, b.category_id, when)
        if state and state[1] > state[0]:
            rows.append((b.category_id, *state))
    return sorted(rows, key=lambda r: r[1] - r[2])


def overspent_line(rows: list[tuple[str, int, int]]) -> str | None:
    if not rows:
        return None
    names = [f"{CATEGORY_NAMES.get(c, c)} (+{thousands(s - p)} F)" for c, p, s in rows[:2]]
    more = f" et {len(rows) - 2} autre{'s' if len(rows) > 3 else ''}" if len(rows) > 2 else ""
    return f"Budget dépassé : {', '.join(names)}{more}."
