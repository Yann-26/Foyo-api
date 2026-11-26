"""Putting an account into a household: sign-up, social sign-in, invitation, invite code.
One place, so the rules (admin succession, history kept) are the same everywhere."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.application import premium
from app.core import clock
from app.core.security import invite_code, new_id
from app.infrastructure.db.models import Household, Invitation, Member, User

MEMBER_COLORS = [0xFF2E7D57, 0xFF3F7DE0, 0xFFD5487A, 0xFFE8763C, 0xFF8A63D2, 0xFF1A9AA0, 0xFFC9A227]
INVITE_TTL_DAYS = 7  # e-mail invitations and invite codes (spec §5.1)

# When the admin leaves, the household goes to the next member with an account, in this order.
SUCCESSION = ("adult", "teen", "helper", "child")


def current_member(db: Session, user: User) -> Member | None:
    return db.scalars(select(Member).where(Member.user_id == user.id, Member.active)).first()


def add_member(db: Session, user: User, household_id: str, role: str) -> Member:
    count = len(db.scalars(select(Member.id).where(Member.household_id == household_id)).all())
    member = Member(
        id=new_id("m"),
        household_id=household_id,
        user_id=user.id,
        first_name=user.first_name,
        role=role,
        color=MEMBER_COLORS[count % len(MEMBER_COLORS)],
        email=user.email,
    )
    db.add(member)
    return member


def leave_current(db: Session, user: User) -> None:
    """The previous membership is kept, inactive, so past tasks and operations stay in that household's
    history. If the user was its admin, the role goes to the next member with an account."""
    old = current_member(db, user)
    if old is None:
        return
    if old.role == "admin":
        heirs = list(
            db.scalars(
                select(Member).where(
                    Member.household_id == old.household_id,
                    Member.active,
                    Member.user_id.is_not(None),
                    Member.id != old.id,
                )
            )
        )
        heirs.sort(key=lambda m: (SUCCESSION.index(m.role) if m.role in SUCCESSION else 99, m.id))
        if heirs:
            heirs[0].role = "admin"
    old.active = False
    old.user_id = None


def move_to(db: Session, user: User, household_id: str, role: str) -> Member:
    leave_current(db, user)
    return add_member(db, user, household_id, role)


def pending_invitation(db: Session, email: str) -> Invitation | None:
    return db.scalars(
        select(Invitation)
        .where(
            Invitation.email == email,
            ~Invitation.accepted,
            ~Invitation.declined,
            Invitation.sent_at > clock.now() - timedelta(days=INVITE_TTL_DAYS),
        )
        .order_by(Invitation.sent_at.desc())
    ).first()


def household_for_code(db: Session, code: str) -> Household | None:
    """A valid invite code (shared by WhatsApp/SMS): exists, not revoked, less than 7 days old."""
    code = code.strip().upper()
    hh = db.scalars(select(Household).where(Household.invite_code == code)).first()
    if hh is None or hh.invite_code_expires_at is None or hh.invite_code_expires_at < clock.now():
        return None
    return hh


def place_new_account(db: Session, user: User, code: str | None = None) -> Member:
    """New account: joins through an invite code, else a pending e-mail invitation, else starts its own
    household as admin."""
    if code:
        hh = household_for_code(db, code)
        if hh is None:
            from app.core.exceptions import bad_request

            raise bad_request("Code d’invitation invalide ou expiré")
        premium.check_limit(hh, "members", premium.members_and_pending(db, hh))
        return add_member(db, user, hh.id, hh.invite_role)
    inv = pending_invitation(db, user.email)
    if inv:
        inv.accepted = True
        return add_member(db, user, inv.household_id, inv.role)
    hh = new_household(db, f"Foyer de {user.first_name}")
    return add_member(db, user, hh.id, "admin")


def new_household(db: Session, name: str) -> Household:
    hh = Household(
        id=new_id("h"),
        name=name,
        invite_code=invite_code(),
        invite_code_expires_at=clock.now() + timedelta(days=INVITE_TTL_DAYS),
    )
    db.add(hh)
    db.flush()
    return hh
