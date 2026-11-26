from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.exceptions import forbidden
from app.core.security import decode_access_token
from app.infrastructure.db.models import Household, Member, User

_bearer = HTTPBearer(auto_error=False)

DB = Annotated[Session, Depends(get_db)]


def get_current_user(db: DB, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> User:
    claims = decode_access_token(creds.credentials) if creds else None
    user = db.get(User, claims[0]) if claims and claims[0] else None
    if user is None or claims[1] != user.token_version:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expirée, reconnectez-vous", {"WWW-Authenticate": "Bearer"})
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@dataclass
class Ctx:
    """Who is calling, and in which household."""

    db: Session
    user: User
    me: Member
    household: Household

    @property
    def hid(self) -> str:
        return self.household.id

    @property
    def is_admin(self) -> bool:
        return self.me.role == "admin"

    @property
    def can_edit(self) -> bool:
        return self.me.role in ("admin", "adult")

    @property
    def sees_health_and_bills(self) -> bool:
        return self.me.role in ("admin", "adult")

    @property
    def sees_all_tasks(self) -> bool:
        """Helpers (aide à domicile) only see the tasks given to them (spec §5.2)."""
        return self.me.role != "helper"

    @property
    def own_stats_only(self) -> bool:
        """Reports and statistics: teens, children and helpers see their own figures (spec §5.2)."""
        return self.me.role in ("teen", "child", "helper")

    def require_export(self) -> None:
        """PDF export: admins and adults; helpers for their own tasks; not teens or children (spec §5.2)."""
        if self.me.role in ("teen", "child"):
            raise forbidden("L’export PDF est réservé aux adultes du foyer")

    def require_edit(self) -> None:
        if not self.can_edit:
            raise forbidden()

    def require_admin(self) -> None:
        if not self.is_admin:
            raise forbidden("Réservé à l’administrateur du foyer")

    def require_module(self, module: str) -> None:
        """Plan gating (spec F20): `tasks` or `finances` module must be in the plan."""
        plan = self.household.plan
        if plan != "complete" and plan != module:
            raise forbidden("Module non inclus dans votre formule")


def get_ctx(db: DB, user: Annotated[User, Depends(get_current_user)]) -> Ctx:
    me = db.scalars(select(Member).where(Member.user_id == user.id, Member.active)).first()
    if me is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Aucun foyer associé à ce compte")
    return Ctx(db=db, user=user, me=me, household=db.get(Household, me.household_id))


CtxDep = Annotated[Ctx, Depends(get_ctx)]
