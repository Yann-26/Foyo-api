"""Demo data: the "Famille Kouassi" household used by the mobile fixtures, dated relative to today.

Log in with awa.kouassi@email.com / foyo1234 (admin) or koffi.k@email.com / foyo1234 (adult).
"""

import logging
import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application.finance_ops import first_due
from app.application.starter_dishes import ensure_starter_dishes
from app.application.tasks import ensure_occurrences
from app.core import clock
from app.core.database import SessionLocal
from app.core.fr import add_months, short_date
from app.core.security import hash_password
from app.infrastructure.db.models import (
    Account,
    Appointment,
    Bill,
    Budget,
    Debt,
    Household,
    IntakeLog,
    Invitation,
    Maintenance,
    MealPlan,
    Medication,
    MedicationStock,
    Member,
    Operation,
    RecurringOperation,
    SavingsGoal,
    ShoppingItem,
    Task,
    Tontine,
    User,
    Vehicle,
)

log = logging.getLogger(__name__)

DEMO_PASSWORD = "foyo1234"
HH = "h-kouassi"

MEMBERS = [
    ("m-awa", "Awa", "admin", 0xFF2E7D57, "awa.kouassi@email.com", None, 320),
    ("m-koffi", "Koffi", "adult", 0xFF3F7DE0, "koffi.k@email.com", None, 210),
    ("m-ines", "Inès", "teen", 0xFFD5487A, "ines.k@email.com", None, 150),
    ("m-junior", "Junior", "child", 0xFFE8763C, None, "m-awa", 40),
    ("m-adjoua", "Adjoua", "helper", 0xFF8A63D2, "adjoua@email.com", None, 0),
]
LOGIN_USERS = {"m-awa", "m-koffi"}

D, W, M = {"kind": "daily"}, (lambda *d: {"kind": "weekly", "weekdays": list(d)}), {"kind": "monthly"}

# (id, name, category, recurrence, minutes, assignee, time, extra)
TASKS = [
    ("t-machine", "Lancer une machine", "lessive", W(1, 4), 15, "m-awa", "09:15", {}),
    ("t-marche", "Marché de Cocody", "courses", W(1), 75, "m-awa", "10:30", {"detail": "liste de 14 articles"}),
    ("t-vitd", "Vitamine D — Junior", "medicaments", D, 2, "m-koffi", "13:00", {"detail": "1 comprimé"}),
    (
        "t-poubelles", "Sortir les poubelles", "poubelles", W(1, 3, 4, 6), 10, "m-ines", "19:00",
        {"rotation": True, "sub_steps": [
            "Vider la poubelle de la cuisine", "Vider celle de la salle de bain", "Sortir le bac et remettre un sac",
        ]},
    ),
    ("t-vaisselle-soir", "Vaisselle du soir", "vaisselle", D, 20, "m-ines", "20:15", {}),
    ("t-devoirs", "Devoirs de Junior", "enfants", W(1, 2, 4, 5), 45, "m-koffi", "17:30", {}),
    ("t-petit-dej", "Petit déjeuner", "cuisine", W(1, 2, 3, 4, 5), 20, "m-koffi", "06:30", {}),
    ("t-vaisselle-matin", "Vaisselle du matin", "vaisselle", D, 15, "m-ines", "07:15", {}),
    ("t-balayer", "Balayer le salon", "menage", D, 15, "m-adjoua", "08:00", {}),
    ("t-diner", "Préparer le dîner", "cuisine", D, 45, "m-awa", "18:30", {}),
    ("t-plan-repas", "Plan des repas de la semaine", "cuisine", W(7), 20, "m-awa", "18:00", {}),
    ("t-frigo", "Nettoyer le réfrigérateur", "cuisine", M, 40, None, None, {"rotation": True}),
    ("t-repassage", "Repassage uniformes", "repassage", W(3, 7), 40, "m-adjoua", "15:00", {}),
    ("t-wc", "Nettoyer les WC", "sdb", W(2, 5), 15, "m-adjoua", "10:00", {}),
    ("t-serpillere", "Serpillière", "menage", W(2, 5), 25, "m-adjoua", "09:00", {}),
    ("t-pneus", "Pression des pneus", "vehicule", M, 15, "m-koffi", None, {}),
    ("t-budget", "Point budget du mois", "factures", M, 30, "m-awa", "20:30", {}),
    ("t-bain", "Bain de Junior", "enfants", D, 20, "m-awa", "19:30", {}),
]

# day index (0 = Monday) → [(slot, time, dish, cook, cost)]
WEEK_MENU = {
    0: [("breakfast", "06:30", "d-omelette", "m-koffi", 1400), ("lunch", "12:30", "d-riz-arachide", "m-adjoua", 5400),
        ("dinner", "19:30", "d-attieke", "m-awa", 7000)],
    1: [("breakfast", "06:30", "d-bouillie", "m-koffi", 1200), ("lunch", "12:30", "d-alloco", "m-adjoua", 2200),
        ("dinner", "19:30", "d-kedjenou", "m-awa", 6500)],
    2: [("breakfast", "06:30", "d-omelette", "m-ines", 1400), ("lunch", "12:30", "d-foutou", "m-adjoua", 3500),
        ("dinner", "19:30", "d-garba", "m-koffi", 4000)],
    3: [("breakfast", "06:30", "d-bouillie", "m-koffi", 1200), ("dinner", "19:30", "d-attieke", "m-awa", 0)],
    4: [("breakfast", "06:30", "d-omelette", "m-koffi", 0), ("lunch", "12:30", "d-riz-arachide", "m-adjoua", 0),
        ("dinner", "19:30", "d-kedjenou", "m-awa", 2700)],
    5: [("breakfast", "08:00", "d-bouillie", "m-ines", 0), ("lunch", "13:00", "d-garba", "m-koffi", 2000),
        ("dinner", "19:30", "d-foutou", "m-awa", 0)],
    6: [("breakfast", "08:00", "d-omelette", "m-koffi", 0), ("lunch", "13:00", "d-alloco", "m-awa", 0),
        ("dinner", "19:30", "d-attieke", "m-koffi", 0)],
}

SHOPPING = [
    ("Poulet", "1,5 kg", 4500, "market", "Kedjenou", True),
    ("Poisson carpe", "4 pièces", 6000, "market", "Attiéké poisson", True),
    ("Tomates fraîches", "1 tas", 600, "market", None, False),
    ("Banane plantain", "2 régimes", 2000, "market", "Alloco", False),
    ("Viande de bœuf", "1 kg", 3500, "market", "Riz sauce arachide", False),
    ("Attiéké", "15 boules", 3000, "market", "3 repas", False),
    ("Igname", "4 tubercules", 4000, "market", "Foutou", False),
    ("Œufs", "30", 3000, "market", "3 petits déjeuners", False),
    ("Riz parfumé", "5 kg", 4500, "supermarket", None, True),
    ("Huile", "1,5 L", 2400, "supermarket", None, False),
    ("Pâte d’arachide", "500 g", 1000, "supermarket", None, False),
    ("Lait concentré", "3 boîtes", 2100, "supermarket", None, False),
    ("Thon", "1 kg", 1400, "market", "Garba", False),
    ("Vitamine D", "1 boîte", 500, "supermarket", "Stock santé bas", False),
]


def _at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(d, time(h, m))


def _clamp(d: date, today: date) -> date:
    return min(d, today)


def seed(db: Session) -> None:
    today = clock.today()
    now = clock.now()
    rnd = random.Random(42)

    # Household & members
    db.add(Household(id=HH, name="Famille Kouassi", invite_code="K7Q2-PA9X", plan="complete", onboarding_completed=True,
                  invite_code_expires_at=now + timedelta(days=7), premium_until=now + timedelta(days=365),
                  premium_source="store", premium_period="yearly", premium_will_renew=True))
    db.flush()
    pw = hash_password(DEMO_PASSWORD)
    for mid, name, role, color, email, managed_by, points in MEMBERS:
        uid = None
        if mid in LOGIN_USERS:
            uid = f"u-{mid[2:]}"
            db.add(User(id=uid, email=email, password_hash=pw, first_name=name, email_verified=True, health_consent_at=now))
            db.flush()
        db.add(Member(id=mid, household_id=HH, user_id=uid, first_name=name, role=role, color=color,
                      email=email, managed_by=managed_by, points=points))
    db.add(Invitation(id="inv-rose", household_id=HH, email="mamie.rose@email.com", role="adult",
                      sent_at=now - timedelta(days=2)))

    # Tasks + 5 weeks of history
    history_start = today - timedelta(days=35)
    for tid, name, cat, rec, mins, assignee, hhmm, extra in TASKS:
        db.add(Task(id=tid, household_id=HH, name=name, category_id=cat, recurrence=rec,
                    start_date=history_start if rec["kind"] != "monthly" else today - timedelta(days=60),
                    estimated_minutes=mins, assignee_id=assignee, time=hhmm, rotation=extra.get("rotation", False),
                    sub_steps=extra.get("sub_steps", []), detail=extra.get("detail")))
    db.commit()
    for back in range(35, 0, -1):
        day = today - timedelta(days=back)
        for occ in ensure_occurrences(db, HH, day).values():
            est = db.get(Task, occ.task_id).estimated_minutes
            r = rnd.random()
            if r < 0.87:
                occ.status = "done"
                occ.actual_seconds = int(est * 60 * rnd.uniform(0.8, 1.25))
                occ.completed_at = datetime.combine(day, time(20, 0))
                occ.sub_steps_done = [True] * len(occ.sub_steps_done or [])
            elif r < 0.9:
                occ.status = "postponed"
            else:
                occ.status = "missed"
        db.commit()
    todays = ensure_occurrences(db, HH, today)
    state = {
        "t-petit-dej": ("done", 22), "t-vaisselle-matin": ("done", 14), "t-balayer": ("done", 18),
        "t-machine": ("running", None),
    }
    for tid, (status, actual) in state.items():
        occ = todays.get(tid)
        if occ is None:
            continue
        occ.status = status
        if status == "done":
            occ.actual_seconds = actual * 60
            occ.completed_at = now
        else:
            occ.started_at = now - timedelta(seconds=767)
            occ.started_by = occ.assignee_id
    if (o := todays.get("t-poubelles")) is not None:
        o.sub_steps_done = [True, True, False]

    # Accounts (balances as in the fixtures)
    for aid, name, typ, bal, private, owner in [
        ("a-cash", "Espèces", "cash", 42500, False, None),
        ("a-wave", "Wave", "wave", 85000, False, None),
        ("a-om", "Orange Money", "orangeMoney", 60000, False, None),
        ("a-bank", "Banque", "bank", 300000, False, None),
        ("a-koffi-perso", "Épargne perso", "bank", 120000, True, "m-koffi"),
    ]:
        db.add(Account(id=aid, household_id=HH, name=name, type=typ, balance=bal, is_private=private, owner_id=owner))
    db.flush()

    # Operations of the current month (fixture day 16 = today)
    def fx(day: int, hhmm: str) -> datetime:
        return _at(_clamp(today - timedelta(days=16 - day), today), hhmm)

    month_start = today.replace(day=1)
    ops = [
        ("o1", "expense", 12500, "alimentation", "a-wave", None, fx(16, "10:40"), "m-awa", "Marché de Cocody", "task"),
        ("o2", "expense", 2200, "transport", "a-cash", None, fx(16, "08:10"), "m-koffi", "Taxi", "manual"),
        ("o3", "income", 350000, "salaire", "a-bank", None, fx(15, "09:00"), "m-koffi", "Salaire Koffi", "recurring"),
        ("o4", "expense", 28500, "factures", "a-wave", None, fx(15, "12:00"), "m-awa", "Électricité", "deadline"),
        ("o5", "expense", 25000, "tontine", "a-om", None, fx(14, "18:00"), "m-awa", "Tontine du bureau", "tontine"),
        ("o6", "expense", 45000, "scolarite", "a-bank", None, fx(14, "10:00"), "m-koffi", "Scolarité Junior", "manual"),
        ("o7", "expense", 8300, "sante", "a-cash", None, fx(13, "17:20"), "m-awa", "Pharmacie", "manual"),
        ("o8", "expense", 2000, "communication", "a-om", None, fx(13, "07:30"), "m-ines", "Crédit téléphone", "manual"),
        ("o9", "transfer", 30000, "autre", "a-bank", "a-wave", fx(12, "09:00"), "m-awa", "Banque → Wave", "manual"),
        ("o10", "expense", 23400, "alimentation", "a-wave", None, fx(12, "18:30"), "m-awa", "Supermarché", "manual"),
        ("o11", "income", 300000, "salaire", "a-bank", None, _at(month_start, "09:00"), "m-awa", "Salaire Awa", "recurring"),
        ("o12", "expense", 62100, "alimentation", "a-cash", None, _at(_clamp(month_start + timedelta(days=3), today), "11:00"), "m-awa", "Marché du mois", "manual"),
        ("o13", "expense", 38800, "transport", "a-cash", None, _at(_clamp(month_start + timedelta(days=4), today), "08:00"), "m-koffi", "Carburant", "manual"),
        ("o14", "expense", 29200, "factures", "a-wave", None, _at(_clamp(month_start + timedelta(days=4), today), "12:00"), "m-awa", "Internet fibre", "deadline"),
        ("o15", "expense", 32000, "loisirs", "a-wave", None, _at(_clamp(month_start + timedelta(days=6), today), "15:00"), "m-koffi", "Sortie en famille", "manual"),
        ("o16", "expense", 3700, "sante", "a-cash", None, _at(_clamp(month_start + timedelta(days=7), today), "10:00"), "m-awa", "Consultation", "manual"),
    ]
    for oid, typ, amt, cat, acc, to, dt, author, label, origin in ops:
        db.add(Operation(id=oid, household_id=HH, type=typ, amount=amt, category_id=cat, account_id=acc,
                         to_account_id=to, date=dt, author_id=author, label=label, origin=origin))

    # Five previous months (so the 6-month chart is filled)
    series = [(620000, 540000), (640000, 580000), (610000, 495000), (700000, 655000), (650000, 520000)]
    split = [("alimentation", 0.40), ("factures", 0.15), ("scolarite", 0.10), ("transport", 0.10),
             ("loisirs", 0.08), ("sante", 0.05), ("tontine", 0.05), ("autre", 0.07)]
    for i, (inc, exp) in enumerate(series):
        m0 = add_months(month_start, i - 5)
        db.add(Operation(id=f"oh{i}-in", household_id=HH, type="income", amount=inc, category_id="salaire",
                         account_id="a-bank", date=_at(m0.replace(day=25), "09:00"), author_id="m-koffi",
                         label="Salaires", origin="recurring"))
        for j, (cat, share) in enumerate(split):
            db.add(Operation(id=f"oh{i}-{j}", household_id=HH, type="expense", amount=int(exp * share),
                             category_id=cat, account_id="a-wave", date=_at(m0.replace(day=5 + 2 * j), "12:00"),
                             author_id="m-awa", label=cat.capitalize(), origin="manual"))

    key = f"{today.year:04d}-{today.month:02d}"
    for cat, planned in [("alimentation", 150000), ("transport", 50000), ("factures", 60000),
                         ("scolarite", 45000), ("loisirs", 25000), ("sante", 30000)]:
        db.add(Budget(id=f"b-{cat}-{key}", household_id=HH, month=key, category_id=cat, planned=planned))

    # Savings, tontine, debts
    db.add_all([
        SavingsGoal(id="g1", household_id=HH, name="Fonds d’urgence", target=1000000, saved=600000, note="50 000 FCFA / mois"),
        SavingsGoal(id="g2", household_id=HH, name=f"Rentrée scolaire {today.year + 1}", target=400000, saved=180000, note="avant août"),
        SavingsGoal(id="g3", household_id=HH, name="Voyage en famille", target=520000, saved=95000, note="libre"),
        Tontine(id="tn1", household_id=HH, name="Tontine du bureau", contribution=25000, member_count=12, my_turn=9,
                start_date=add_months(month_start, -4), pay_day=30),
        Debt(id="d1", household_id=HH, direction="lent", person="Yao", amount=50000,
             note=f"À récupérer le {short_date(today + timedelta(days=14))}"),
        Debt(id="d2", household_id=HH, direction="borrowed", person="Crédit réfrigérateur", amount=80000,
             note="4 mensualités restantes", repaid=20000),
        RecurringOperation(id="ro-salaire", household_id=HH, type="income", amount=350000, category_id="salaire",
                           account_id="a-bank", label="Salaire Koffi", author_id="m-koffi", every_months=1, day=15,
                           next_date=first_due(15, today + timedelta(days=1))),
        RecurringOperation(id="ro-loyer", household_id=HH, type="expense", amount=150000, category_id="logement",
                           account_id="a-bank", label="Loyer", author_id="m-awa", every_months=1, day=5,
                           next_date=first_due(5, today + timedelta(days=1))),
    ])

    # Bills & vehicle
    db.add_all([
        Bill(id="b1", household_id=HH, label="Électricité", amount=28500, due_date=today + timedelta(days=4), every_months=1),
        Bill(id="b2", household_id=HH, label="Eau", amount=9200, due_date=today + timedelta(days=6), every_months=1),
        Bill(id="b3", household_id=HH, label="Internet fibre", amount=25000,
             due_date=_clamp(month_start + timedelta(days=4), today), paid_at=_clamp(month_start + timedelta(days=4), today)),
        Bill(id="b4", household_id=HH, label="Loyer", amount=79300, due_date=today + timedelta(days=14), category_id="logement"),
        Vehicle(id="v1", household_id=HH, name="Toyota Corolla", mileage=84300),
    ])
    db.flush()
    vidange = today + timedelta(days=21)
    assurance, visite = today + timedelta(days=79), today + timedelta(days=90)
    db.add_all([
        Maintenance(id="mt1", vehicle_id="v1", label="Vidange", detail=f"Dans 700 km ou le {short_date(vidange)}",
                    due_km=85000, interval_km=10000, due_date=vidange, period_days=180),
        Maintenance(id="mt2", vehicle_id="v1", label="Révision complète", detail="À 90 000 km",
                    due_km=90000, interval_km=15000),
        Maintenance(id="mt3", vehicle_id="v1", label="Assurance & visite technique",
                    detail=f"{short_date(assurance)} {assurance.year} · {short_date(visite)} {visite.year}",
                    due_date=assurance, period_days=365),
    ])

    # Health
    db.add_all([
        Medication(id="i1", household_id=HH, member_id="m-junior", time="08:00", product="Sirop", dose="5 ml",
                   course_start=today - timedelta(days=3), course_end=today + timedelta(days=6)),
        Medication(id="i2", household_id=HH, member_id="m-junior", time="13:00", product="Vitamine D", dose="1 comprimé"),
        Medication(id="i3", household_id=HH, member_id="m-junior", time="20:00", product="Sirop", dose="5 ml",
                   course_start=today - timedelta(days=3), course_end=today + timedelta(days=6)),
        Medication(id="i4", household_id=HH, member_id="m-koffi", time="07:30", product="Tension", dose="1 comprimé"),
        Medication(id="i5", household_id=HH, member_id="m-awa", time="21:00", product="Fer", dose="1 gélule"),
        MedicationStock(id="st1", household_id=HH, product="Vitamine D", remaining=5, unit="comprimés"),
        MedicationStock(id="st2", household_id=HH, product="Sirop", remaining=12, unit="doses"),
        Appointment(id="ap1", household_id=HH, member_id="m-koffi", title="Bilan annuel — Koffi",
                    at=_at(today + timedelta(days=23), "09:00")),
        Appointment(id="ap2", household_id=HH, member_id="m-junior", title="Rappel de vaccin — Junior",
                    plan_label=f"Janvier {today.year + 1} · à planifier"),
        Appointment(id="ap3", household_id=HH, member_id="m-ines", title="Dentiste — Inès",
                    at=_at(today + timedelta(days=12), "10:30")),
    ])
    db.flush()
    db.add_all([
        IntakeLog(id="il1", medication_id="i1", date=today, taken_at=_at(today, "08:04")),
        IntakeLog(id="il2", medication_id="i4", date=today, taken_at=_at(today, "07:41")),
    ])

    # Menus (starter dishes are shared by every household)
    ensure_starter_dishes(db)
    db.get(Household, HH).favorite_dishes = ["d-kedjenou"]
    monday = today - timedelta(days=today.weekday())
    for idx, meals in WEEK_MENU.items():
        for k, (slot, hhmm, dish, cook, cost) in enumerate(meals):
            db.add(MealPlan(id=f"mp-{idx}-{k}", household_id=HH, date=monday + timedelta(days=idx), slot=slot,
                            time=hhmm, dish_id=dish, cook_id=cook, cost=cost))
    for k, (name, qty, price, place, source, checked) in enumerate(SHOPPING, start=1):
        db.add(ShoppingItem(id=f"s{k}", household_id=HH, week_start=monday, name=name, quantity=qty, price=price,
                            place=place, source=source, checked=checked, auto=source is not None))
    db.commit()


def seed_if_empty() -> None:
    with SessionLocal() as db:
        if db.scalars(select(Household.id)).first() is not None:
            return
        log.info("Empty database: loading the demo household (Famille Kouassi)")
        seed(db)


__all__ = ["DEMO_PASSWORD", "seed", "seed_if_empty"]
