"""Free version limits and Premium features (spec §9). The subscription belongs to the household:
one payment unlocks every member."""

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.infrastructure.db.models import Account, Budget, Household, Invitation, Member, Operation, Task

TRIAL_DAYS = 7

# Free version (spec §9).
FREE_LIMITS = {
    "members": 3,
    "recurring_tasks": 10,
    "accounts": 2,
    "operations_per_month": 50,
    "budgets": 3,
}
_LABELS = {
    "members": "membres",
    "recurring_tasks": "tâches récurrentes",
    "accounts": "comptes",
    "operations_per_month": "opérations par mois",
    "budgets": "budgets",
}


def premium_active(hh: Household) -> bool:
    return hh.premium_until is not None and hh.premium_until > clock.now()


def _payment_required(text: str) -> HTTPException:
    # 402: the app shows the message with a way to the Premium screen.
    return HTTPException(status.HTTP_402_PAYMENT_REQUIRED, f"{text} Essai gratuit de {TRIAL_DAYS} jours dans Réglages.")


def require_premium(hh: Household, what: str) -> None:
    if not premium_active(hh):
        raise _payment_required(f"{what} : réservé à Premium.")


def check_limit(hh: Household, name: str, used: int) -> None:
    """Refuses one more item when the free version's limit is reached."""
    if not premium_active(hh) and used >= FREE_LIMITS[name]:
        raise _payment_required(f"Version gratuite : {FREE_LIMITS[name]} {_LABELS[name]} au maximum.")


def module_links_active(hh: Household) -> bool:
    """Links between modules (shopping → expense, bill → operation): Complet *and* Premium (spec §9)."""
    return hh.plan == "complete" and premium_active(hh)


# ── Usage counters ──


def members_and_pending(db: Session, hh: Household) -> int:
    members = db.scalar(select(func.count(Member.id)).where(Member.household_id == hh.id, Member.active)) or 0
    pending = db.scalar(
        select(func.count(Invitation.id)).where(
            Invitation.household_id == hh.id, ~Invitation.accepted, ~Invitation.declined
        )
    ) or 0
    return members + pending


def recurring_tasks(db: Session, hh: Household) -> int:
    tasks = db.scalars(select(Task.recurrence).where(Task.household_id == hh.id, Task.active))
    return sum(1 for r in tasks if (r or {}).get("kind", "daily") != "once")


def accounts(db: Session, hh: Household) -> int:
    return db.scalar(select(func.count(Account.id)).where(Account.household_id == hh.id)) or 0


def operations_in_month(db: Session, hh: Household, when: datetime) -> int:
    start = datetime(when.year, when.month, 1)
    end = datetime(when.year + (when.month == 12), when.month % 12 + 1, 1)
    return db.scalar(
        select(func.count(Operation.id)).where(
            Operation.household_id == hh.id, Operation.date >= start, Operation.date < end
        )
    ) or 0


def budgets_in_month(db: Session, hh: Household, month: str) -> int:
    return db.scalar(select(func.count(Budget.id)).where(Budget.household_id == hh.id, Budget.month == month)) or 0


def usage(db: Session, hh: Household) -> dict:
    now = clock.now()
    return {
        "members": members_and_pending(db, hh),
        "recurring_tasks": recurring_tasks(db, hh),
        "accounts": accounts(db, hh),
        "operations_per_month": operations_in_month(db, hh, now),
        "budgets": budgets_in_month(db, hh, f"{now.year:04d}-{now.month:02d}"),
    }
