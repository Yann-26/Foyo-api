"""Scheduled pushes. Each job looks at "now" and sends what is due; NotificationLog keys make
every reminder go out exactly once, even if the worker restarts or runs twice."""

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.notifications import notify_member, notify_user, recipient_user
from app.application.tasks import POINTS_PER_TASK, ensure_occurrences, minutes_of
from app.core.fr import short_date, thousands
from app.infrastructure.db.models import Bill, Household, IntakeLog, Medication, Member, Occurrence, Task, User
from app.infrastructure.notifications.fcm_client import PushMessage

# A reminder is still worth sending this long after its time (worker restart, deploy…), not later.
GRACE = timedelta(minutes=30)
# A task without a time is reminded at 8:00 (spec §5.3).
DEFAULT_TIME = "08:00"
LATE_AFTER = timedelta(minutes=60)  # "En retard" to the person responsible…
LATE_ADMIN_AFTER = timedelta(hours=3)  # …and to the admin(s) after 3 h (spec §5.4)
DAILY_REPORT_AT = (21, 0)
BILLS_AT = (18, 30)
BILL_DAYS = (7, 3, 1, 0)  # J-7, J-3, J-1 and on the day
STILL_RUNNING_FACTOR = 3  # "Toujours en cours ?" at 3× the estimate…
AUTO_STOP_AFTER = timedelta(hours=4)  # …and the timer stops itself after 4 h (spec §5.5)


def _at(day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def task_message(t: Task, member: Member | None) -> PushMessage:
    name = member.first_name if member else ""
    return PushMessage(
        t.name,
        f"{name + ', c' if name else 'C'}’est l’heure. Durée estimée : {t.estimated_minutes} min.",
        {"route": f"/task/{t.id}/run", "task_id": t.id},
    )


def medication_message(med: Medication, member: Member | None) -> PushMessage:
    return PushMessage(
        f"Médicament à {med.time} — {member.first_name if member else ''}".strip(" —"),
        f"{med.product} · {med.dose}.",
        {"route": "/health", "medication_id": med.id},
    )


def _health_consent(db: Session, member_id: str) -> bool:
    """Medication reminders only reach someone who agreed to health data (spec: consent)."""
    user = recipient_user(db, db.get(Member, member_id))
    return user is not None and user.health_consent_at is not None


def _due(at: datetime, now: datetime) -> bool:
    return at <= now < at + GRACE


def _households(db: Session, module: str | None = None) -> list[Household]:
    rows = list(db.scalars(select(Household)))
    return [h for h in rows if module is None or h.plan in ("complete", module)]


def task_reminders(db: Session, now: datetime) -> int:
    """At the task's time minus its reminder offset (screen 10)."""
    sent = 0
    today = now.date()
    for hh in _households(db, "tasks"):
        occs = ensure_occurrences(db, hh.id, today)
        tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(occs.keys())))}
        for occ in occs.values():
            t = tasks[occ.task_id]
            if occ.status != "todo":
                continue
            if not _due(_at(today, t.time or DEFAULT_TIME) - timedelta(minutes=t.reminder_offset or 0), now):
                continue
            member = db.get(Member, occ.assignee_id) if occ.assignee_id else None
            msg = task_message(t, member)
            sent += notify_member(db, occ.assignee_id, msg, kind="task", key=f"task:{occ.id}", now=now)
    return sent


def late_tasks(db: Session, now: datetime) -> int:
    """A task still to do 60 min after its time: "En retard" to the person responsible, and to the
    admin(s) 3 h after. Not muted for phones that schedule reminders themselves (kind "late")."""
    sent = 0
    today = now.date()
    for hh in _households(db, "tasks"):
        occs = ensure_occurrences(db, hh.id, today)
        tasks = {t.id: t for t in db.scalars(select(Task).where(Task.id.in_(occs.keys())))}
        admins = list(db.scalars(select(Member).where(Member.household_id == hh.id, Member.active, Member.role == "admin")))
        for occ in occs.values():
            t = tasks[occ.task_id]
            if occ.status != "todo" or not t.time:
                continue
            due = _at(today, t.time)
            member = db.get(Member, occ.assignee_id) if occ.assignee_id else None
            if _due(due + LATE_AFTER, now):
                msg = PushMessage(f"En retard : {t.name}", f"Prévue à {t.time}. Démarrer ou reporter ?", {"route": f"/task/{t.id}/run"})
                sent += notify_member(db, occ.assignee_id, msg, kind="late", key=f"late:{occ.id}", now=now)
            if _due(due + LATE_ADMIN_AFTER, now):
                who = member.first_name if member else "personne"
                msg = PushMessage(f"En retard : {t.name}", f"Prévue à {t.time} pour {who}, toujours pas faite.", {"route": "/today"})
                for admin in admins:
                    if admin.id != occ.assignee_id:
                        sent += notify_member(db, admin.id, msg, kind="late", key=f"late-admin:{occ.id}", now=now)
    return sent


def medication_reminders(db: Session, now: datetime) -> int:
    """At each intake time, unless already confirmed. Not muted by quiet hours."""
    sent = 0
    today = now.date()
    for med in db.scalars(select(Medication)):
        if med.course_start and today < med.course_start or med.course_end and today > med.course_end:
            continue
        if not _due(_at(today, med.time), now):
            continue
        if db.scalars(select(IntakeLog.id).where(IntakeLog.medication_id == med.id, IntakeLog.date == today)).first():
            continue
        if not _health_consent(db, med.member_id):
            continue
        msg = medication_message(med, db.get(Member, med.member_id))
        sent += notify_member(db, med.member_id, msg, kind="medication", key=f"med:{med.id}:{today}", now=now)
    return sent


def daily_reports(db: Session, now: datetime) -> int:
    """21:00 summary of the day (screen 13), to every member with an account who wants it."""
    if not _due(_at(now.date(), f"{DAILY_REPORT_AT[0]:02d}:{DAILY_REPORT_AT[1]:02d}"), now):
        return 0
    sent = 0
    today = now.date()
    for hh in _households(db, "tasks"):
        rows = db.execute(
            select(Occurrence, Task)
            .join(Task, Task.id == Occurrence.task_id)
            .where(Occurrence.household_id == hh.id, Occurrence.date == today)
        ).all()
        if not rows:
            continue
        done = [(o, t) for o, t in rows if o.status == "done"]
        minutes = sum(minutes_of(o, t) for o, t in done)
        body = f"{len(done)}/{len(rows)} tâches faites aujourd’hui · {minutes} min au total."
        # Complet plan: an overspent budget is part of the evening report, for the adults (spec §5.11).
        money = None
        if hh.plan == "complete":
            from app.application import finance_ops

            money = finance_ops.overspent_line(finance_ops.overspent(db, hh.id, now))
        for m in db.scalars(select(Member).where(Member.household_id == hh.id, Member.active, Member.user_id.is_not(None))):
            text = f"{body} {money}" if money and m.role in ("admin", "adult") else body
            msg = PushMessage("Rapport du jour", text, {"route": "/reports/daily"})
            sent += notify_user(db, db.get(User, m.user_id), msg, kind="report", key=f"report:{today}", now=now)
    return sent


def bill_reminders(db: Session, now: datetime) -> int:
    """18:30, a week before, three days before, the day before and on the due date — to admins and adults."""
    if not _due(_at(now.date(), f"{BILLS_AT[0]:02d}:{BILLS_AT[1]:02d}"), now):
        return 0
    sent = 0
    today = now.date()
    for bill in db.scalars(select(Bill).where(Bill.paid_at.is_(None))):
        days = (bill.due_date - today).days
        if days not in BILL_DAYS:
            continue
        when = {0: "aujourd’hui", 1: "demain"}.get(days, f"dans {days} jours")
        msg = PushMessage(
            f"Facture {bill.label.lower()} {when}",
            f"{thousands(bill.amount)} FCFA · échéance le {short_date(bill.due_date)}.",
            {"route": "/deadlines"},
        )
        payers = db.scalars(
            select(Member).where(Member.household_id == bill.household_id, Member.active, Member.role.in_(("admin", "adult")))
        )
        for m in payers:
            sent += notify_user(db, recipient_user(db, m), msg, kind="bill", key=f"bill:{bill.id}:{days}", now=now)
    return sent


def long_timers(db: Session, now: datetime) -> int:
    """A forgotten timer: ask at 3× the estimate, then stop it at 4 h and keep the estimate as duration."""
    sent = 0
    for occ in db.scalars(select(Occurrence).where(Occurrence.status == "running", Occurrence.started_at.is_not(None))):
        t = db.get(Task, occ.task_id)
        elapsed = now - occ.started_at
        who = occ.started_by or occ.assignee_id
        if elapsed >= AUTO_STOP_AFTER:
            occ.status = "done"
            occ.completed_at = now
            occ.actual_seconds = t.estimated_minutes * 60
            occ.duration_estimated = True
            occ.sub_steps_done = [True] * len(t.sub_steps or [])
            occ.started_by = None
            if (doer := db.get(Member, occ.assignee_id or who)) is not None:
                doer.points += POINTS_PER_TASK
            db.commit()
            msg = PushMessage(
                f"Chronomètre arrêté : {t.name}",
                f"Il tournait depuis 4 h. Durée retenue : {t.estimated_minutes} min (estimée).",
                {"route": "/today"},
            )
            sent += notify_member(db, who, msg, kind="timer", key=f"auto-stop:{occ.id}", now=now)
        elif elapsed >= timedelta(minutes=t.estimated_minutes * STILL_RUNNING_FACTOR):
            minutes = int(elapsed.total_seconds() // 60)
            msg = PushMessage(
                f"Toujours en cours ? {t.name}",
                f"Le chronomètre tourne depuis {minutes} min (prévu : {t.estimated_minutes} min). Terminez ou arrêtez-le.",
                {"route": f"/task/{t.id}/run", "task_id": t.id},
            )
            sent += notify_member(db, who, msg, kind="timer", key=f"still-running:{occ.id}", now=now)
    return sent


APPOINTMENT_EVE_AT = (18, 30)
APPOINTMENT_BEFORE = timedelta(hours=2)


def appointment_reminders(db: Session, now: datetime) -> int:
    """The evening before (18:30) and 2 h before, to the person (or the admin who manages their profile),
    if they agreed to health data."""
    from app.infrastructure.db.models import Appointment

    sent = 0
    eve = _due(_at(now.date(), f"{APPOINTMENT_EVE_AT[0]:02d}:{APPOINTMENT_EVE_AT[1]:02d}"), now)
    tomorrow = now.date() + timedelta(days=1)
    for a in db.scalars(select(Appointment).where(Appointment.at.is_not(None), Appointment.at > now)):
        member = db.get(Member, a.member_id)
        if member is None or not _health_consent(db, a.member_id):
            continue
        who = f" ({member.first_name})" if member.user_id is None else ""
        hhmm = f"{a.at.hour}:{a.at.minute:02d}"
        if eve and a.at.date() == tomorrow:
            msg = PushMessage(f"Demain {hhmm} · {a.title}{who}", "Rendez-vous de santé.", {"route": "/health"})
            sent += notify_member(db, a.member_id, msg, kind="appointment", key=f"appt:{a.id}:eve", now=now)
        if _due(a.at - APPOINTMENT_BEFORE, now):
            msg = PushMessage(f"Dans 2 h · {a.title}{who}", f"Rendez-vous à {hhmm}.", {"route": "/health"})
            sent += notify_member(db, a.member_id, msg, kind="appointment", key=f"appt:{a.id}:soon", now=now)
    return sent


TONTINE_AT = (18, 30)
TONTINE_DAYS = (1, 0)  # the day before and on the contribution day


def recurring_operations(db: Session, now: datetime) -> int:
    """Rent, salary…: written on their day, the author is told; budget alerts follow for expenses."""
    from app.application import finance_ops

    written = finance_ops.run_recurring(db, now.date())
    sent = 0
    for r, op in written:
        sign = "−" if op.type == "expense" else ("+" if op.type == "income" else "")
        msg = PushMessage(
            f"{op.label or finance_ops.CATEGORY_NAMES.get(op.category_id, 'Opération')} enregistré",
            f"{sign}{thousands(op.amount)} F · opération récurrente du {short_date(op.date.date())}.",
            {"route": "/finances/operations"},
        )
        sent += notify_member(db, r.author_id, msg, kind="finance", key=f"recurring:{op.id}", now=now)
        if op.type == "expense":
            sent += finance_ops.notify_budget(db, op.household_id, op.category_id, op.date, now=now)
    return sent


def tontine_reminders(db: Session, now: datetime) -> int:
    """18:30, the day before and on the contribution day; says when it is the household's turn."""
    from app.application import finance_ops
    from app.infrastructure.db.models import Tontine

    if not _due(_at(now.date(), f"{TONTINE_AT[0]:02d}:{TONTINE_AT[1]:02d}"), now):
        return 0
    sent, today = 0, now.date()
    for t in db.scalars(select(Tontine)):
        pay = finance_ops.due_date(today.year, today.month, t.pay_day)
        days = (pay - today).days
        if days not in TONTINE_DAYS:
            continue
        months = (today.year - t.start_date.year) * 12 + today.month - t.start_date.month
        if months < 0 or months >= t.member_count:
            continue  # not started yet, or every turn done
        when = "aujourd’hui" if days == 0 else "demain"
        body = f"{thousands(t.contribution)} F à verser."
        if months + 1 == t.my_turn:
            body += f" C’est votre tour : vous recevez {thousands(t.contribution * t.member_count)} F."
        msg = PushMessage(f"Tontine {t.name} : cotisation {when}", body, {"route": "/finances/savings"})
        for mid in finance_ops.alert_recipients(db, t.household_id):
            sent += notify_member(db, mid, msg, kind="finance", key=f"tontine:{t.id}:{pay}:{days}", now=now)
    return sent


TELEMETRY_KEEP = timedelta(days=90)


def purge_telemetry(db: Session, now: datetime) -> int:
    """Usage events and crash reports are kept 90 days."""
    from sqlalchemy import delete

    from app.infrastructure.db.models import CrashReport, TelemetryEvent

    cut = now - TELEMETRY_KEEP
    n = db.execute(delete(TelemetryEvent).where(TelemetryEvent.at < cut)).rowcount
    n += db.execute(delete(CrashReport).where(CrashReport.at < cut)).rowcount
    db.commit()
    return n


def purge_idempotency_keys(db: Session, now: datetime) -> int:
    from app.api.idempotency import purge_old_keys

    return purge_old_keys(db, now) if now.minute == 0 else 0  # once an hour is plenty


JOBS = (task_reminders, late_tasks, long_timers, medication_reminders, daily_reports, bill_reminders,
        recurring_operations, tontine_reminders, appointment_reminders, purge_idempotency_keys, purge_telemetry)
