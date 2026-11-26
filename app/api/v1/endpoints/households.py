from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, status
from sqlalchemy import select

from app.api.deps import CtxDep
from app.application import premium
from app.application.membership import INVITE_TTL_DAYS, MEMBER_COLORS
from app.application.notifications import notify_member_later, notify_user_later
from app.application.tasks import member_load
from app.core import clock
from app.core.config import get_settings
from app.core.exceptions import bad_request, conflict, not_found
from app.core.security import invite_code, new_id
from app.infrastructure.db.models import ALL_CATEGORIES, Invitation, Member, User
from app.infrastructure.email import email_client
from app.schemas.households import (
    AdminTransferIn,
    HouseholdCreateIn,
    HouseholdPatchIn,
    InvitationIn,
    InviteLinkIn,
    ManagedMemberIn,
    ResendIn,
)

router = APIRouter(tags=["households"])


def member_json(m: Member) -> dict:
    return {
        "id": m.id,
        "first_name": m.first_name,
        "role": m.role,
        "color": m.color,
        "email": m.email,
        "managed_by": m.managed_by,
        "has_account": m.user_id is not None,
    }


def _sent_label(sent_at: datetime, now: datetime) -> str:
    delta = now - sent_at
    if delta < timedelta(minutes=1):
        return "Envoyée à l’instant"
    if delta < timedelta(hours=1):
        return f"Envoyée il y a {int(delta.total_seconds() // 60)} min"
    if delta < timedelta(days=1):
        return f"Envoyée il y a {int(delta.total_seconds() // 3600)} h"
    if delta.days == 1:
        return "Envoyée hier"
    return f"Envoyée il y a {delta.days} jours"


def invitation_json(i: Invitation, now: datetime | None = None) -> dict:
    now = now or clock.now()
    return {
        "email": i.email,
        "role": i.role,
        "sent_label": _sent_label(i.sent_at, now),
        "expires_in_days": max(0, INVITE_TTL_DAYS - (now - i.sent_at).days),
    }


def _code_valid(hh) -> bool:  # noqa: ANN001
    return hh.invite_code_expires_at is not None and hh.invite_code_expires_at > clock.now()


def _members(ctx: CtxDep) -> list[Member]:
    return list(ctx.db.scalars(select(Member).where(Member.household_id == ctx.hid, Member.active).order_by(Member.id)))


def household_json(ctx: CtxDep) -> dict:
    db, hh = ctx.db, ctx.household
    members = _members(ctx)
    names = {m.id: m.first_name for m in members}
    today = clock.today()
    load = {r["member_id"]: r for r in member_load(db, hh.id, members, today - timedelta(days=6), today)}
    activity = []
    for m in members:
        if ctx.own_stats_only and m.id != ctx.me.id:
            activity.append({"member_id": m.id, "summary": ROLE_LABELS.get(m.role, "Admin").capitalize()})
            continue
        summary = f"{load[m.id]['task_count']} tâches cette semaine"
        if m.managed_by:
            summary += f" · profil géré par {names.get(m.managed_by, '—')}"
        elif m.role == "helper":
            summary += " · accès limité à ses tâches"
        activity.append({"member_id": m.id, "summary": summary})
    invitations = db.scalars(
        select(Invitation)
        .where(Invitation.household_id == hh.id, ~Invitation.accepted, ~Invitation.declined)
        .order_by(Invitation.sent_at)
    )
    return {
        "id": hh.id,
        "name": hh.name,
        "plan": hh.plan,
        "categories": hh.categories,
        "onboarding_completed": hh.onboarding_completed,
        "invite_link": f"{get_settings().public_base_url}/i/{hh.invite_code}" if _code_valid(hh) else None,
        "invite_code": hh.invite_code if _code_valid(hh) else None,
        "invite_role": hh.invite_role,
        "invite_expires_in_days": max(0, (hh.invite_code_expires_at - clock.now()).days) if _code_valid(hh) else 0,
        "members": [member_json(m) for m in members],
        "invitations": [invitation_json(i) for i in invitations],
        "activity": activity,
    }


ROLE_LABELS = {"adult": "adulte", "teen": "ado", "child": "enfant", "helper": "aide à domicile"}
RESEND_SECONDS = 60


def _invitation_email(ctx: CtxDep, inv: Invitation) -> tuple[str, str]:
    hh, me = ctx.household.name, ctx.me.first_name
    has_account = ctx.db.scalars(select(User.id).where(User.email == inv.email)).first() is not None
    how = (
        "Vous avez déjà un compte Foyo : ouvrez l’application, l’invitation vous attend dans Réglages."
        if has_account
        else f"Pour accepter : installez Foyo et créez votre compte avec cette adresse e-mail ({inv.email}). "
        "Vous rejoindrez automatiquement le foyer."
    )
    subject = f"{me} vous invite à rejoindre « {hh} » sur Foyo"
    body = (
        f"Bonjour,\n\n"
        f"{me} vous invite à rejoindre le foyer « {hh} » sur Foyo, l’application qui organise les tâches "
        f"et le budget de la maison, en tant qu’{ROLE_LABELS.get(inv.role, inv.role)}.\n\n"
        f"{how}\n\n"
        f"Cette invitation est valable {INVITE_TTL_DAYS} jours. "
        f"Si vous ne connaissez pas {me}, ignorez simplement ce message."
    )
    return subject, body


def _send_invitation(ctx: CtxDep, background: BackgroundTasks, inv: Invitation) -> None:
    subject, body = _invitation_email(ctx, inv)
    background.add_task(email_client.deliver, inv.email, subject, body)
    invitee = ctx.db.scalars(select(User).where(User.email == inv.email)).first()
    if invitee:  # already has the app: a push too
        background.add_task(
            notify_user_later,
            invitee.id,
            f"Invitation : {ctx.household.name}",
            f"{ctx.me.first_name} vous invite à rejoindre « {ctx.household.name} ».",
            "/settings",
            "invitation",
        )


def _invite(ctx: CtxDep, body: InvitationIn) -> Invitation:
    """Creates the invitation, or refreshes the pending one for the same address."""
    email = body.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise bad_request("Adresse e-mail invalide")
    if body.role == "admin":
        raise bad_request("Un seul administrateur par foyer")
    if ctx.db.scalars(select(Member.id).where(Member.household_id == ctx.hid, Member.active, Member.email == email)).first():
        raise conflict(f"{email} fait déjà partie du foyer")
    inv = ctx.db.scalars(
        select(Invitation).where(Invitation.household_id == ctx.hid, Invitation.email == email, ~Invitation.accepted)
    ).first()
    if inv is None:
        premium.check_limit(ctx.household, "members", premium.members_and_pending(ctx.db, ctx.household))
        inv = Invitation(
            id=new_id("inv"), household_id=ctx.hid, email=email, role=body.role, sent_at=clock.now(),
            invited_by=ctx.me.id,
        )
        ctx.db.add(inv)
    else:
        inv.role, inv.sent_at, inv.invited_by, inv.declined = body.role, clock.now(), ctx.me.id, False
    return inv


@router.get("/households/current")
def get_current(ctx: CtxDep) -> dict:
    return household_json(ctx)


@router.get("/households/{household_id}")
def get_household(household_id: str, ctx: CtxDep) -> dict:
    if household_id != ctx.hid:
        raise not_found("Foyer")
    return household_json(ctx)


@router.post("/households", status_code=status.HTTP_201_CREATED)
def create_household(body: HouseholdCreateIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    """Screen 04. Sign-up already created the caller's household: this names it and sends invitations."""
    ctx.require_admin()
    ctx.household.name = body.name.strip()
    invitations = [_invite(ctx, inv) for inv in body.invitations]
    ctx.db.commit()
    for inv in invitations:
        _send_invitation(ctx, background, inv)
    return household_json(ctx)


@router.patch("/households/current")
def patch_household(body: HouseholdPatchIn, ctx: CtxDep) -> dict:
    ctx.require_admin()
    hh = ctx.household
    if body.name is not None:
        hh.name = body.name.strip()
    if body.categories is not None:
        unknown = set(body.categories) - set(ALL_CATEGORIES)
        if unknown:
            raise bad_request(f"Catégories inconnues : {', '.join(sorted(unknown))}")
        hh.categories = list(body.categories)
    if body.onboarding_completed is not None:
        hh.onboarding_completed = body.onboarding_completed
    ctx.db.commit()
    return household_json(ctx)


@router.post("/households/current/transfer-admin")
def transfer_admin(body: AdminTransferIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    """Hands the admin role to another member with an account; the current admin becomes an adult."""
    ctx.require_admin()
    target = ctx.db.get(Member, body.member_id)
    if target is None or target.household_id != ctx.hid or not target.active:
        raise not_found("Membre")
    if target.id == ctx.me.id:
        raise bad_request("Vous êtes déjà l’administrateur")
    if target.user_id is None:
        raise bad_request(f"{target.first_name} n’a pas de compte Foyo : invitez-le d’abord par e-mail")
    target.role = "admin"
    ctx.me.role = "adult"
    ctx.db.commit()
    background.add_task(
        notify_member_later,
        target.id,
        f"Vous êtes administrateur de « {ctx.household.name} »",
        f"{ctx.me.first_name} vous a confié la gestion du foyer.",
        "/household",
        "admin",
    )
    return household_json(ctx)


@router.post("/households/current/invite-link")
def renew_invite_link(body: InviteLinkIn, ctx: CtxDep) -> dict:
    """New code (the old one stops working), valid 7 days, for the given role (spec §5.1)."""
    ctx.require_admin()
    if body.role == "admin":
        raise bad_request("Un seul administrateur par foyer")
    hh = ctx.household
    hh.invite_code = invite_code()
    hh.invite_role = body.role
    hh.invite_code_expires_at = clock.now() + timedelta(days=INVITE_TTL_DAYS)
    ctx.db.commit()
    return household_json(ctx)


@router.delete("/households/current/invite-link")
def revoke_invite_link(ctx: CtxDep) -> dict:
    """The shared link stops working at once."""
    ctx.require_admin()
    hh = ctx.household
    hh.invite_code = invite_code()  # the old code no longer exists
    hh.invite_code_expires_at = None
    ctx.db.commit()
    return household_json(ctx)


@router.get("/members")
def list_members(ctx: CtxDep) -> list[dict]:
    return [member_json(m) for m in _members(ctx)]


@router.post("/members", status_code=status.HTTP_201_CREATED)
def add_managed_member(body: ManagedMemberIn, ctx: CtxDep) -> dict:
    ctx.require_admin()
    premium.check_limit(ctx.household, "members", premium.members_and_pending(ctx.db, ctx.household))

    m = Member(
        id=new_id("m"),
        household_id=ctx.hid,
        first_name=body.first_name,
        role=body.role,
        color=body.color or MEMBER_COLORS[len(_members(ctx)) % len(MEMBER_COLORS)],
        managed_by=ctx.me.id,
    )
    ctx.db.add(m)
    ctx.db.commit()
    return member_json(m)


@router.post("/households/{household_id}/invitations", status_code=status.HTTP_201_CREATED)
def invite(household_id: str, body: InvitationIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    if household_id not in (ctx.hid, "current"):
        raise not_found("Foyer")
    ctx.require_admin()
    inv = _invite(ctx, body)
    ctx.db.commit()
    _send_invitation(ctx, background, inv)
    return invitation_json(inv)


@router.post("/households/{household_id}/invitations/resend")
def resend(household_id: str, body: ResendIn, ctx: CtxDep, background: BackgroundTasks) -> dict:
    """Sends the e-mail again and restarts the 7-day validity."""
    if household_id not in (ctx.hid, "current"):
        raise not_found("Foyer")
    ctx.require_admin()
    inv = ctx.db.scalars(
        select(Invitation).where(
            Invitation.household_id == ctx.hid,
            Invitation.email == body.email.strip().lower(),
            ~Invitation.accepted,
            ~Invitation.declined,
        )
    ).first()
    if inv is None:
        raise not_found("Invitation")
    if clock.now() - inv.sent_at < timedelta(seconds=RESEND_SECONDS):
        raise bad_request("Invitation déjà envoyée il y a moins d’une minute")
    inv.sent_at = clock.now()
    ctx.db.commit()
    _send_invitation(ctx, background, inv)
    return invitation_json(inv)
