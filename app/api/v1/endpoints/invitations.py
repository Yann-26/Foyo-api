"""Invitations received by a signed-in user (joining another household with an existing account)."""

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, get_current_user
from app.api.v1.endpoints.households import ROLE_LABELS, member_json
from app.application import premium
from app.application.membership import INVITE_TTL_DAYS, household_for_code, move_to
from app.core import clock
from app.core.exceptions import bad_request, not_found
from app.infrastructure.db.models import Household, Invitation, Member, User

router = APIRouter(prefix="/invitations", tags=["invitations"])

UserDep = Annotated[User, Depends(get_current_user)]

# When the admin leaves, the household goes to the next member with an account, in this order.
SUCCESSION = ("adult", "teen", "helper", "child")


def _current_member(db: DB, user: User) -> Member | None:
    return db.scalars(select(Member).where(Member.user_id == user.id, Member.active)).first()


def _pending(db: DB, user: User):
    """Valid invitations for the user's address, except to the household they're already in."""
    me = _current_member(db, user)
    q = select(Invitation).where(
        Invitation.email == user.email,
        ~Invitation.accepted,
        ~Invitation.declined,
        Invitation.sent_at > clock.now() - timedelta(days=INVITE_TTL_DAYS),
    )
    if me:
        q = q.where(Invitation.household_id != me.household_id)
    return q


def _get(db: DB, user: User, invitation_id: str) -> Invitation:
    inv = db.scalars(_pending(db, user).where(Invitation.id == invitation_id)).first()
    if inv is None:
        raise not_found("Invitation")
    return inv


class JoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)


@router.get("/code/{code}")
def code_preview(code: str, db: DB) -> dict:
    """Shown before joining: which household a shared code leads to."""
    hh = household_for_code(db, code)
    if hh is None:
        raise not_found("Code d’invitation")
    return {"household_name": hh.name, "role": hh.invite_role, "role_label": ROLE_LABELS.get(hh.invite_role, hh.invite_role)}


@router.post("/join")
def join_with_code(body: JoinIn, db: DB, user: UserDep) -> dict:
    """An existing account joins through a shared link / code (moves household, like an invitation)."""
    hh = household_for_code(db, body.code)
    if hh is None:
        raise bad_request("Code d’invitation invalide ou expiré")
    me = _current_member(db, user)
    if me is not None and me.household_id == hh.id:
        raise bad_request("Vous faites déjà partie de ce foyer")
    premium.check_limit(hh, "members", premium.members_and_pending(db, hh))
    member = move_to(db, user, hh.id, hh.invite_role)
    db.commit()
    return {"household_id": hh.id, "household_name": hh.name, "plan": hh.plan, "member": member_json(member)}


@router.get("/received")
def received(db: DB, user: UserDep) -> list[dict]:
    now = clock.now()
    out = []
    for inv in db.scalars(_pending(db, user).order_by(Invitation.sent_at.desc())):
        hh = db.get(Household, inv.household_id)
        inviter = db.get(Member, inv.invited_by) if inv.invited_by else None
        out.append(
            {
                "id": inv.id,
                "household_id": hh.id,
                "household_name": hh.name,
                "invited_by": inviter.first_name if inviter else None,
                "role": inv.role,
                "role_label": ROLE_LABELS.get(inv.role, inv.role),
                "expires_in_days": max(0, INVITE_TTL_DAYS - (now - inv.sent_at).days),
            }
        )
    return out


@router.post("/{invitation_id}/accept")
def accept(invitation_id: str, db: DB, user: UserDep) -> dict:
    """Moves the user to the inviting household.

    The previous membership is kept, inactive, so past tasks and operations stay in that household's
    history. If the user was its admin, the role goes to the next member with an account.
    """
    inv = _get(db, user, invitation_id)
    member = move_to(db, user, inv.household_id, inv.role)
    inv.accepted = True
    db.commit()
    hh = db.get(Household, inv.household_id)
    return {"household_id": hh.id, "household_name": hh.name, "plan": hh.plan, "member": member_json(member)}


@router.post("/{invitation_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
def decline(invitation_id: str, db: DB, user: UserDep) -> None:
    inv = _get(db, user, invitation_id)
    if inv.accepted:
        raise bad_request("Invitation déjà acceptée")
    inv.declined = True
    db.commit()
