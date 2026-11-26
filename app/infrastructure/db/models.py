"""SQLAlchemy ORM models (spec data model §7). Ids are short prefixed strings (`m-…`, `t-…`)."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base

ALL_CATEGORIES = [
    "cuisine", "vaisselle", "menage", "lessive", "repassage", "courses", "sdb",
    "poubelles", "reparations", "factures", "vehicule", "sante", "medicaments", "enfants",
]


def _fk(target: str, nullable: bool = False) -> Mapped:
    return mapped_column(String(40), ForeignKey(target, ondelete="CASCADE"), nullable=nullable, index=True)


# ── Account & household (F1) ──────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("google_sub", name="uq_users_google_sub"),
        UniqueConstraint("apple_sub", name="uq_users_apple_sub"),
    )
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    first_name: Mapped[str] = mapped_column(String(60))
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Google / Apple sign-in: the provider's stable user id (spec §5.1).
    google_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    apple_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Bumped on password reset: tokens carrying an older version are rejected (signs out every device).
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # Brute-force protection on sign-in.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Notification preferences (screen 20 · Rappels).
    push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    quiet_hours: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # 22:00 → 6:00, except medications
    daily_report: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # every evening at 21:00
    # Explicit consent to health data (medications, appointments); None = not given or withdrawn.
    health_consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Device(Base):
    """A phone that receives push notifications (Firebase Cloud Messaging registration token)."""

    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(512), unique=True)
    platform: Mapped[str] = mapped_column(String(10))  # android | ios
    # The app schedules task / medication reminders on the phone (they fire offline):
    # the server then skips those pushes for this device, so nothing arrives twice.
    local_reminders: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)


class NotificationLog(Base):
    """One row per notification sent: the key (e.g. `task:<occurrence>`) makes every reminder go out once."""

    __tablename__ = "notification_logs"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_notification_user_key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(120))
    sent_at: Mapped[datetime] = mapped_column(DateTime)


class IdempotencyKey(Base):
    """Answer already given to an action sent with an `Idempotency-Key` header (offline replays, retries)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_idempotency_user_key"),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PasswordReset(Base):
    """A 6-digit code sent by e-mail. Only its hash is stored."""

    __tablename__ = "password_resets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Household(Base):
    __tablename__ = "households"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    invite_code: Mapped[str] = mapped_column(String(20), unique=True)
    # The shareable link / code (WhatsApp, SMS): valid 7 days, revocable, joins with this role (spec §5.1).
    invite_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invite_role: Mapped[str] = mapped_column(String(20), default="adult", server_default="adult")
    plan: Mapped[str] = mapped_column(String(20), default="complete")  # tasks | finances | complete
    # Premium while premium_until is in the future: 7-day trial or store subscription (RevenueCat).
    premium_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    premium_source: Mapped[str | None] = mapped_column(String(10), nullable=True)  # trial | store
    premium_period: Mapped[str | None] = mapped_column(String(10), nullable=True)  # monthly | yearly
    premium_will_renew: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    categories: Mapped[list] = mapped_column(JSON, default=lambda: list(ALL_CATEGORIES))
    # Starter or own dishes marked as favourites by this household.
    favorite_dishes: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    user_id: Mapped[str | None] = mapped_column(String(40), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    first_name: Mapped[str] = mapped_column(String(60))
    role: Mapped[str] = mapped_column(String(20))  # admin | adult | teen | child | helper
    color: Mapped[int] = mapped_column(BigInteger)  # ARGB, above the 32-bit signed range
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    managed_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    # False once the person left for another household; the row is kept for the history (tasks, reports).
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Invitation(Base):
    __tablename__ = "invitations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    invited_by: Mapped[str | None] = mapped_column(String(40), nullable=True)  # member id
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    declined: Mapped[bool] = mapped_column(Boolean, default=False)


# ── Tasks (F2–F6) ─────────────────────────────────────────────────────────────


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    name: Mapped[str] = mapped_column(String(60))
    category_id: Mapped[str] = mapped_column(String(30), index=True)
    recurrence: Mapped[dict] = mapped_column(JSON)  # {kind, weekdays, interval}
    start_date: Mapped[date] = mapped_column(Date)
    estimated_minutes: Mapped[int] = mapped_column(Integer)
    assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    rotation: Mapped[bool] = mapped_column(Boolean, default=False)
    reminder_offset: Mapped[int] = mapped_column(Integer, default=0)
    sub_steps: Mapped[list] = mapped_column(JSON, default=list)  # list[str]
    detail: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Occurrence(Base):
    __tablename__ = "occurrences"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = _fk("tasks.id")
    household_id: Mapped[str] = _fk("households.id")
    date: Mapped[date] = mapped_column(Date, index=True)
    assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="todo")  # todo|running|done|postponed|missed
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Ticked without the timer: the duration is the estimate, shown as "estimée" (spec §5.5).
    duration_estimated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    sub_steps_done: Mapped[list] = mapped_column(JSON, default=list)  # list[bool]
    # Who pressed "Démarrer": one running task per person (spec §5.5).
    started_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Optional proof at validation: a short note and/or a photo (key in the file storage).
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    photo_key: Mapped[str | None] = mapped_column(String(80), nullable=True)


# ── Finances (F15–F19) ────────────────────────────────────────────────────────


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    name: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(20))  # cash|wave|orangeMoney|mtnMomo|moov|bank|other
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    type: Mapped[str] = mapped_column(String(10))  # expense | income | transfer
    amount: Mapped[int] = mapped_column(BigInteger)
    category_id: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str] = mapped_column(String(40), ForeignKey("accounts.id", ondelete="CASCADE"))
    to_account_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    date: Mapped[datetime] = mapped_column(DateTime, index=True)
    author_id: Mapped[str] = mapped_column(String(40))
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="manual")
    # The transfer fee is its own expense, linked to the operation it belongs to (deleted with it).
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    receipt_key: Mapped[str | None] = mapped_column(String(80), nullable=True)  # photo of the receipt


class RecurringOperation(Base):
    """Rent, salary, school fees…: written to the accounts automatically on their day (spec F16)."""

    __tablename__ = "recurring_operations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    type: Mapped[str] = mapped_column(String(10))
    amount: Mapped[int] = mapped_column(BigInteger)
    fee: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    category_id: Mapped[str] = mapped_column(String(30))
    account_id: Mapped[str] = mapped_column(String(40), ForeignKey("accounts.id", ondelete="CASCADE"))
    to_account_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    author_id: Mapped[str] = mapped_column(String(40))
    every_months: Mapped[int] = mapped_column(Integer, default=1)  # 1 monthly, 3 quarterly, 12 yearly
    day: Mapped[int] = mapped_column(Integer)  # day of the month (31 = last day)
    next_date: Mapped[date] = mapped_column(Date, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class Budget(Base):
    __tablename__ = "budgets"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    category_id: Mapped[str] = mapped_column(String(30))
    planned: Mapped[int] = mapped_column(Integer)


class SavingsGoal(Base):
    __tablename__ = "savings_goals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    name: Mapped[str] = mapped_column(String(80))
    target: Mapped[int] = mapped_column(Integer)
    saved: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Tontine(Base):
    __tablename__ = "tontines"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    name: Mapped[str] = mapped_column(String(80))
    contribution: Mapped[int] = mapped_column(Integer)
    member_count: Mapped[int] = mapped_column(Integer)
    my_turn: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)  # month of turn 1
    pay_day: Mapped[int] = mapped_column(Integer, default=30)


class Debt(Base):
    __tablename__ = "debts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    direction: Mapped[str] = mapped_column(String(10))  # lent | borrowed
    person: Mapped[str] = mapped_column(String(80))
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(120), default="")
    repaid: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # partial repayments


# ── Deadlines (F11) ───────────────────────────────────────────────────────────


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    label: Mapped[str] = mapped_column(String(80))
    amount: Mapped[int] = mapped_column(Integer)
    due_date: Mapped[date] = mapped_column(Date)
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    category_id: Mapped[str] = mapped_column(String(30), default="factures")
    # Recurring bill (electricity every month, insurance every year): paying it creates the next one.
    every_months: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    name: Mapped[str] = mapped_column(String(80))
    mileage: Mapped[int] = mapped_column(Integer)


class Maintenance(Base):
    __tablename__ = "maintenances"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    vehicle_id: Mapped[str] = _fk("vehicles.id")
    label: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(String(120), default="")
    due_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    interval_km: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_done_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_cost: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── Health (F10) ──────────────────────────────────────────────────────────────


class Medication(Base):
    """A scheduled intake (one row per member × time × product)."""

    __tablename__ = "medications"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    member_id: Mapped[str] = mapped_column(String(40))
    time: Mapped[str] = mapped_column(String(5))
    product: Mapped[str] = mapped_column(String(80))
    dose: Mapped[str] = mapped_column(String(40))
    course_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    course_end: Mapped[date | None] = mapped_column(Date, nullable=True)


class IntakeLog(Base):
    __tablename__ = "intake_logs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    medication_id: Mapped[str] = _fk("medications.id")
    date: Mapped[date] = mapped_column(Date, index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime)


class MedicationStock(Base):
    __tablename__ = "medication_stocks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    product: Mapped[str] = mapped_column(String(80))
    remaining: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str] = mapped_column(String(30))
    alert_threshold: Mapped[int] = mapped_column(Integer, default=7)


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    member_id: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(120))
    at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    plan_label: Mapped[str | None] = mapped_column(String(80), nullable=True)  # when not scheduled yet


# ── Menus (F14) ───────────────────────────────────────────────────────────────


class Dish(Base):
    __tablename__ = "dishes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("households.id", ondelete="CASCADE"), nullable=True, index=True
    )  # null = starter catalogue shared by every household
    name: Mapped[str] = mapped_column(String(80))
    slot: Mapped[str] = mapped_column(String(12))  # breakfast | lunch | dinner | snack
    servings: Mapped[int] = mapped_column(Integer)
    prep_minutes: Mapped[int] = mapped_column(Integer)
    subtitle: Mapped[str | None] = mapped_column(String(120), nullable=True)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    ingredients: Mapped[list] = mapped_column(JSON, default=list)


class MealPlan(Base):
    __tablename__ = "meal_plans"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    date: Mapped[date] = mapped_column(Date, index=True)
    slot: Mapped[str] = mapped_column(String(12))
    time: Mapped[str] = mapped_column(String(5))
    dish_id: Mapped[str] = mapped_column(String(40), ForeignKey("dishes.id", ondelete="CASCADE"))
    cook_id: Mapped[str] = mapped_column(String(40))
    cost: Mapped[int] = mapped_column(Integer, default=0)


class ShoppingItem(Base):
    __tablename__ = "shopping_items"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    household_id: Mapped[str] = _fk("households.id")
    week_start: Mapped[date] = mapped_column(Date, index=True)
    name: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[str] = mapped_column(String(40))
    price: Mapped[int] = mapped_column(Integer, default=0)
    place: Mapped[str] = mapped_column(String(15), default="market")  # market | supermarket
    source: Mapped[str | None] = mapped_column(String(80), nullable=True)
    checked: Mapped[bool] = mapped_column(Boolean, default=False)
    # Generated from the week's menu (replaced on regeneration); False = added by hand (kept).
    auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


# ── Telemetry (anonymous: a random install id, no account) ───────────────────


class TelemetryEvent(Base):
    """Usage statistics, only from phones where the person turned them on."""

    __tablename__ = "telemetry_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    install_id: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(40), index=True)
    props: Mapped[dict] = mapped_column(JSON, default=dict)
    app_version: Mapped[str] = mapped_column(String(20))
    platform: Mapped[str] = mapped_column(String(10))
    at: Mapped[datetime] = mapped_column(DateTime, index=True)


class CrashReport(Base):
    __tablename__ = "crash_reports"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    install_id: Mapped[str] = mapped_column(String(40), index=True)
    app_version: Mapped[str] = mapped_column(String(20))
    platform: Mapped[str] = mapped_column(String(10))
    error: Mapped[str] = mapped_column(String(500))
    stack: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)  # same crash = same fingerprint
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
