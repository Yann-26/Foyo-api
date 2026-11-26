from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "adult", "teen", "child", "helper"]


class InvitationIn(BaseModel):
    email: str = Field(max_length=255)
    role: Role = "adult"


class HouseholdCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    invitations: list[InvitationIn] = []


class HouseholdPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    categories: list[str] | None = None
    onboarding_completed: bool | None = None


class ResendIn(BaseModel):
    email: str


class ManagedMemberIn(BaseModel):
    """A profile without e-mail (child without smartphone), managed by an admin."""

    first_name: str = Field(min_length=1, max_length=60)
    role: Role = "child"
    color: int | None = None


class AdminTransferIn(BaseModel):
    member_id: str


class InviteLinkIn(BaseModel):
    role: Role = "adult"
