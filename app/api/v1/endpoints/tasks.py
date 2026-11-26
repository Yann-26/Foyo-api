from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.api.deps import Ctx, CtxDep
from app.application import premium
from app.application.notifications import notify_member_later
from app.application.tasks import (
    POINTS_PER_TASK,
    day_occurrences,
    ensure_occurrences,
    minutes_of,
    streak,
    task_json,
)
from app.core import clock
from app.core.exceptions import bad_request, conflict, forbidden, not_found
from app.core.fr import thousands
from app.core.security import new_id
from app.domain.services.recurrence_service import Recurrence, is_due
from app.infrastructure.db.models import (
    Bill,
    MealPlan,
    Medication,
    MedicationStock,
    Member,
    Occurrence,
    Task,
)
from app.infrastructure.email import email_client
from app.infrastructure.storage import files
from app.schemas.tasks import CompleteIn, OccurrencePatchIn, StartIn, TaskIn, TaskPatchIn

router = APIRouter(tags=["tasks"])

TEMPLATES = {
    "cuisine": ["Dégivrer le congélateur", "Nettoyer le four", "Ranger les placards", "Préparer les goûters"],
    "vaisselle": ["Vaisselle du matin", "Vaisselle du soir", "Ranger la vaisselle"],
    "menage": ["Balayer", "Serpillière", "Dépoussiérer", "Changer les draps", "Vitres"],
    "lessive": ["Lancer une machine", "Étendre", "Plier et ranger"],
    "repassage": ["Repassage de la semaine", "Uniformes scolaires"],
    "courses": ["Marché hebdomadaire", "Supermarché", "Gaz", "Eau"],
    "sdb": ["Nettoyer les WC", "Nettoyer la douche", "Nettoyer le lavabo", "Changer les serviettes"],
    "poubelles": ["Sortir les poubelles", "Laver le bac"],
    "reparations": ["Ampoule", "Robinet", "Serrure", "Appeler le plombier"],
    "factures": ["Électricité", "Eau", "Internet", "Loyer", "Scolarité", "Point budget du mois"],
    "vehicule": ["Vidange", "Révision", "Visite technique", "Assurance", "Pression des pneus", "Lavage"],
    "sante": ["Bilan annuel", "Dentiste", "Ophtalmologue", "Vaccins"],
    "medicaments": ["Prise quotidienne", "Traitement à durée limitée", "Renouvellement"],
    "enfants": ["Devoirs", "Bain", "Coucher", "Goûter", "Trajet école", "Activités"],
}


def _visible(ctx: Ctx, assignee_id: str | None) -> bool:
    return ctx.me.role != "helper" or assignee_id == ctx.me.id


def _get_task(ctx: Ctx, task_id: str) -> Task:
    t = ctx.db.get(Task, task_id)
    if t is None or t.household_id != ctx.hid or not t.active:
        raise not_found("Tâche")
    return t


def _check_member(ctx: Ctx, member_id: str | None) -> None:
    if member_id is None:
        return
    m = ctx.db.get(Member, member_id)
    if m is None or m.household_id != ctx.hid or not m.active:
        raise bad_request("Membre inconnu dans ce foyer")


def _today_occurrence(ctx: Ctx, task: Task, day: date | None = None) -> Occurrence:
    today = day or clock.today()
    occ = ensure_occurrences(ctx.db, ctx.hid, today).get(task.id)
    if occ is None:
        # Not scheduled today (e.g. started from the category screen): create an ad-hoc occurrence.
        occ = Occurrence(
            id=new_id("oc"),
            task_id=task.id,
            household_id=ctx.hid,
            date=today,
            assignee_id=task.assignee_id or ctx.me.id,
            sub_steps_done=[False] * len(task.sub_steps or []),
        )
        ctx.db.add(occ)
        ctx.db.flush()
    return occ


def _notify_assignee(ctx: Ctx, background: BackgroundTasks, t: Task) -> None:
    """Push "Nouvelle tâche" to whoever was given the task (not to yourself)."""
    if not t.assignee_id or t.assignee_id == ctx.me.id:
        return
    when = f" · {t.time}" if t.time else ""
    assignee = ctx.db.get(Member, t.assignee_id)
    if assignee is not None and assignee.user_id and assignee.email:
        background.add_task(
            email_client.deliver,
            assignee.email,
            f"Foyo — nouvelle tâche : {t.name}",
            f"Bonjour {assignee.first_name},\n\n{ctx.me.first_name} vous a confié « {t.name} » "
            f"({Recurrence.label(t.recurrence)}{when}).\nVous la retrouverez dans l’application Foyo, onglet Aujourd’hui.",
        )
    background.add_task(
        notify_member_later,
        t.assignee_id,
        f"Nouvelle tâche : {t.name}",
        f"{ctx.me.first_name} vous l’a confiée{when}.",
        "/today",
        "assignment",
    )


# ── Today (screen 05) ─────────────────────────────────────────────────────────


@router.get("/tasks/today")
def today(ctx: CtxDep, day: date | None = Query(default=None, alias="date")) -> dict:
    ctx.require_module("tasks")
    day = day or clock.today()
    occs = ensure_occurrences(ctx.db, ctx.hid, day)
    tasks = {t.id: t for t in ctx.db.scalars(select(Task).where(Task.id.in_(occs.keys())))}
    now = clock.now()
    items = [task_json(tasks[tid], o, now) for tid, o in occs.items() if _visible(ctx, o.assignee_id)]
    items.sort(key=lambda x: x["time"] or "99")
    mine = [o for o in occs.values() if o.assignee_id == ctx.me.id]
    minutes_today = sum(minutes_of(o, tasks[o.task_id]) for o in mine if o.status == "done")
    cur_streak, _ = streak(ctx.db, ctx.hid, ctx.me.id, day)
    return {
        "date": day.isoformat(),
        "summary": {
            "my_done": sum(o.status == "done" for o in mine),
            "my_planned": len(mine),
            "streak_days": cur_streak,
            "minutes_today": minutes_today,
            # A helper doesn't see the others' tasks, so not their totals either.
            "household_done": sum(o.status == "done" for o in (occs.values() if ctx.sees_all_tasks else mine)),
            "household_planned": len(occs) if ctx.sees_all_tasks else len(mine),
        },
        "tasks": items,
    }


# ── Task CRUD (screen 08) ─────────────────────────────────────────────────────


@router.get("/tasks")
def list_tasks(ctx: CtxDep, category_id: str | None = None) -> list[dict]:
    q = select(Task).where(Task.household_id == ctx.hid, Task.active)
    if category_id:
        q = q.where(Task.category_id == category_id)
    occs = ensure_occurrences(ctx.db, ctx.hid, clock.today())
    return [
        task_json(t, occs.get(t.id))
        for t in ctx.db.scalars(q.order_by(Task.time))
        if _visible(ctx, t.assignee_id)
    ]


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
def create_task(body: TaskIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    ctx.require_module("tasks")
    ctx.require_edit()
    if body.recurrence.kind != "once":
        premium.check_limit(ctx.household, "recurring_tasks", premium.recurring_tasks(ctx.db, ctx.household))
    if body.rotation:
        premium.require_premium(ctx.household, "La rotation automatique")
    if body.category_id not in TEMPLATES:
        raise bad_request("Catégorie inconnue")
    if body.recurrence.kind in ("weekly", "custom") and not body.recurrence.weekdays:
        raise bad_request("Choisissez au moins un jour")
    _check_member(ctx, body.assignee_id)
    t = Task(
        id=new_id("t"),
        household_id=ctx.hid,
        name=body.name.strip(),
        category_id=body.category_id,
        recurrence=body.recurrence.model_dump(),
        start_date=clock.today(),
        estimated_minutes=body.estimated_minutes,
        assignee_id=body.assignee_id,
        time=body.time,
        rotation=body.rotation,
        reminder_offset=body.reminder_offset,
        sub_steps=[s.label for s in body.sub_steps],
        detail=body.detail,
    )
    ctx.db.add(t)
    ctx.db.commit()
    _notify_assignee(ctx, background, t)
    occ = ensure_occurrences(ctx.db, ctx.hid, clock.today()).get(t.id)
    return task_json(t, occ)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, ctx: CtxDep) -> dict:
    t = _get_task(ctx, task_id)
    occ = ensure_occurrences(ctx.db, ctx.hid, clock.today()).get(t.id)
    if not _visible(ctx, (occ.assignee_id if occ else None) or t.assignee_id):
        raise not_found("Tâche")
    return task_json(t, occ)


@router.patch("/tasks/{task_id}")
def patch_task(task_id: str, body: TaskPatchIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    ctx.require_edit()
    t = _get_task(ctx, task_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("rotation") and not t.rotation:
        premium.require_premium(ctx.household, "La rotation automatique")
    if (t.recurrence or {}).get("kind") == "once" and (data.get("recurrence") or {}).get("kind", "once") != "once":
        premium.check_limit(ctx.household, "recurring_tasks", premium.recurring_tasks(ctx.db, ctx.household))
    reassigned = "assignee_id" in data and data["assignee_id"] != t.assignee_id
    if "assignee_id" in data:
        _check_member(ctx, data["assignee_id"])
    for k, v in data.items():
        setattr(t, k, v)
    ctx.db.commit()
    if reassigned:
        _notify_assignee(ctx, background, t)
    return task_json(t, ensure_occurrences(ctx.db, ctx.hid, clock.today()).get(t.id))


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: str, ctx: CtxDep) -> None:
    ctx.require_edit()
    t = _get_task(ctx, task_id)
    t.active = False  # history (occurrences, reports) is kept
    ctx.db.commit()


# ── Timer: start / complete (screens 10–12) ───────────────────────────────────


def _start(ctx: Ctx, task: Task, occ: Occurrence, started_at: datetime | None = None) -> dict:
    if not _visible(ctx, occ.assignee_id):
        raise forbidden()
    if occ.status not in ("done", "running"):
        # One running task per person (spec §5.5).
        other = ctx.db.scalar(
            select(Occurrence).where(
                Occurrence.household_id == ctx.hid,
                Occurrence.status == "running",
                Occurrence.started_by == ctx.me.id,
                Occurrence.id != occ.id,
            )
        )
        if other is not None:
            name = ctx.db.get(Task, other.task_id).name
            raise conflict(f"« {name} » est déjà en cours. Terminez-la d’abord : une tâche à la fois.")
        occ.status = "running"
        occ.started_by = ctx.me.id
        occ.started_at = occ.started_at or (clock.to_local(started_at) if started_at else clock.now())
    ctx.db.commit()
    return task_json(task, occ)


def _stop(ctx: Ctx, task: Task, occ: Occurrence) -> dict:
    """"Arrêter" without validating: the timer is dropped, the task is to do again."""
    if not _visible(ctx, occ.assignee_id):
        raise forbidden()
    if occ.status == "running":
        occ.status = "todo"
        occ.started_at = None
        occ.started_by = None
    ctx.db.commit()
    return task_json(task, occ)


def _complete(
    ctx: Ctx,
    task: Task,
    occ: Occurrence,
    actual_seconds: int | None,
    completed_at: datetime | None = None,
    note: str | None = None,
) -> dict:
    if not _visible(ctx, occ.assignee_id):
        raise forbidden()
    when = clock.to_local(completed_at) if completed_at else clock.now()
    was_done = occ.status == "done"
    doer = ctx.db.get(Member, occ.assignee_id) if occ.assignee_id else ctx.me
    doer = doer or ctx.me
    # Two members ticked it (e.g. both offline): the first timestamped validation wins (spec §7).
    if not (was_done and occ.completed_at and occ.completed_at <= when):
        occ.status = "done"
        occ.completed_at = when
        occ.duration_estimated = actual_seconds is None
        occ.actual_seconds = task.estimated_minutes * 60 if actual_seconds is None else actual_seconds
        occ.sub_steps_done = [True] * len(task.sub_steps or [])
        occ.started_by = None
    if note is not None:
        occ.note = note.strip() or None
    earned = 0 if was_done else POINTS_PER_TASK
    doer.points += earned
    ctx.db.commit()

    occs = ensure_occurrences(ctx.db, ctx.hid, occ.date)
    tasks = {t.id: t for t in ctx.db.scalars(select(Task).where(Task.id.in_(occs.keys())))}
    nxt = sorted(
        (o for o in occs.values() if o.status == "todo" and o.assignee_id == occ.assignee_id and o.id != occ.id),
        key=lambda o: tasks[o.task_id].time or "99",
    )
    cur_streak, _ = streak(ctx.db, ctx.hid, doer.id, occ.date)
    return {
        "task": task_json(task, occ),
        "actual_seconds": occ.actual_seconds,
        "streak_days": cur_streak,
        "points_earned": earned,
        "total_points": doer.points,
        "next_task": task_json(tasks[nxt[0].task_id], nxt[0]) if nxt else None,
    }


@router.post("/tasks/{task_id}/start")
def start_task(task_id: str, ctx: CtxDep, body: StartIn | None = None) -> dict:
    t = _get_task(ctx, task_id)
    body = body or StartIn()
    return _start(ctx, t, _today_occurrence(ctx, t, body.date), body.started_at)


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: str, body: CompleteIn, ctx: CtxDep) -> dict:
    t = _get_task(ctx, task_id)
    return _complete(ctx, t, _today_occurrence(ctx, t, body.date), body.actual_seconds, body.completed_at, body.note)


def _get_occ(ctx: Ctx, occurrence_id: str) -> tuple[Occurrence, Task]:
    o = ctx.db.get(Occurrence, occurrence_id)
    if o is None or o.household_id != ctx.hid:
        raise not_found("Occurrence")
    return o, ctx.db.get(Task, o.task_id)


@router.post("/occurrences/{occurrence_id}/start")
def start_occurrence(occurrence_id: str, ctx: CtxDep) -> dict:
    o, t = _get_occ(ctx, occurrence_id)
    return _start(ctx, t, o)


@router.post("/tasks/{task_id}/stop")
def stop_task(task_id: str, ctx: CtxDep, day: date | None = Query(default=None, alias="date")) -> dict:
    t = _get_task(ctx, task_id)
    return _stop(ctx, t, _today_occurrence(ctx, t, day))


@router.post("/occurrences/{occurrence_id}/stop")
def stop_occurrence(occurrence_id: str, ctx: CtxDep) -> dict:
    o, t = _get_occ(ctx, occurrence_id)
    return _stop(ctx, t, o)


@router.post("/occurrences/{occurrence_id}/photo")
async def upload_photo(occurrence_id: str, ctx: CtxDep, file: UploadFile) -> dict:
    """Proof photo at validation (spec §5.5). Replaces the previous one."""
    o, t = _get_occ(ctx, occurrence_id)
    if not _visible(ctx, o.assignee_id):
        raise forbidden()
    try:
        key = files.save(f"tasks/{ctx.hid}", await file.read(files.MAX_BYTES + 1), file.content_type)
    except files.InvalidUpload as e:
        raise bad_request(str(e)) from e
    files.delete(o.photo_key)
    o.photo_key = key
    ctx.db.commit()
    return task_json(t, o)


@router.get("/occurrences/{occurrence_id}/photo")
def get_photo(occurrence_id: str, ctx: CtxDep) -> FileResponse:
    o, _ = _get_occ(ctx, occurrence_id)
    if not _visible(ctx, o.assignee_id) or not o.photo_key or (path := files.path_of(o.photo_key)) is None:
        raise not_found("Photo")
    return FileResponse(path, media_type=files.media_type(o.photo_key), headers={"Cache-Control": "private, max-age=86400"})


@router.post("/occurrences/{occurrence_id}/complete")
def complete_occurrence(occurrence_id: str, body: CompleteIn, ctx: CtxDep) -> dict:
    o, t = _get_occ(ctx, occurrence_id)
    return _complete(ctx, t, o, body.actual_seconds, body.completed_at, body.note)


@router.patch("/occurrences/{occurrence_id}")
def patch_occurrence(occurrence_id: str, body: OccurrencePatchIn, ctx: CtxDep) -> dict:
    o, t = _get_occ(ctx, occurrence_id)
    if not _visible(ctx, o.assignee_id):
        raise forbidden()
    if body.sub_steps_done is not None:
        if len(body.sub_steps_done) != len(t.sub_steps or []):
            raise bad_request("Nombre de sous-étapes incorrect")
        o.sub_steps_done = body.sub_steps_done
    if body.status is not None:
        o.status = body.status
    if "note" in body.model_fields_set:
        o.note = (body.note or "").strip() or None
    ctx.db.commit()
    return task_json(t, o)


# ── Categories (screens 06–07) ────────────────────────────────────────────────


def _alerts(ctx: Ctx) -> set[str]:
    db, today = ctx.db, clock.today()
    alerts = set()
    if db.scalars(
        select(Bill.id).where(Bill.household_id == ctx.hid, Bill.paid_at.is_(None), Bill.due_date <= today + timedelta(days=7))
    ).first():
        alerts.add("factures")
    if db.scalars(
        select(MedicationStock.id).where(
            MedicationStock.household_id == ctx.hid, MedicationStock.remaining <= MedicationStock.alert_threshold
        )
    ).first():
        alerts.add("medicaments")
    yesterday = today - timedelta(days=1)
    missed = db.execute(
        select(Task.category_id)
        .join(Occurrence, Occurrence.task_id == Task.id)
        .where(Occurrence.household_id == ctx.hid, Occurrence.date == yesterday, Occurrence.status.in_(("todo", "missed")))
    ).scalars()
    alerts.update(missed)
    return alerts


@router.get("/categories")
def categories(ctx: CtxDep) -> list[dict]:
    rows = dict(
        ctx.db.execute(
            select(Task.category_id, func.count(Task.id))
            .where(Task.household_id == ctx.hid, Task.active)
            .group_by(Task.category_id)
        ).all()
    )
    alerts = _alerts(ctx)
    return [
        {"category_id": c, "task_count": rows.get(c, 0), "has_alert": c in alerts}
        for c in ctx.household.categories
    ]


@router.get("/categories/{category_id}")
def category_detail(category_id: str, ctx: CtxDep) -> dict:
    if category_id not in TEMPLATES:
        raise not_found("Catégorie")
    today = clock.today()
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    tasks = list(
        ctx.db.scalars(
            select(Task).where(Task.household_id == ctx.hid, Task.active, Task.category_id == category_id).order_by(Task.time)
        )
    )
    occs = [
        (o, t)
        for o, t in day_occurrences(ctx.db, ctx.hid, week_start, week_end)
        if t.category_id == category_id and (not ctx.own_stats_only or o.assignee_id == ctx.me.id)
    ]
    done = [(o, t) for o, t in occs if o.status == "done"]
    planned = sum(
        1
        for t in tasks
        if not ctx.own_stats_only or t.assignee_id == ctx.me.id
        for i in range(7)
        if is_due(t.recurrence, t.start_date, week_start + timedelta(days=i))
    )
    total = sum(minutes_of(o, t) for o, t in done)
    today_occ = ensure_occurrences(ctx.db, ctx.hid, today)
    data = {
        "category_id": category_id,
        "done_this_week": len(done),
        "planned_this_week": max(planned, len(done)),
        "avg_minutes": round(total / len(done)) if done else 0,
        "total_minutes": total,
        "templates": TEMPLATES[category_id],
        "tasks": [task_json(t, today_occ.get(t.id)) for t in tasks if _visible(ctx, t.assignee_id)],
        "menu_summary": None,
    }
    if category_id == "cuisine":
        meals = list(
            ctx.db.scalars(
                select(MealPlan).where(
                    MealPlan.household_id == ctx.hid, MealPlan.date >= week_start, MealPlan.date <= week_end
                )
            )
        )
        cost = sum(m.cost for m in meals)
        data["menu_summary"] = f"{len(meals)}/21 repas planifiés · ≈ {thousands(cost)} FCFA"
    if category_id == "medicaments":
        data["detail"] = f"{ctx.db.scalar(select(func.count(Medication.id)).where(Medication.household_id == ctx.hid))} prises programmées"
    return data
