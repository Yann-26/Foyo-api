from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    target: int = Field(gt=0)
    saved: int = Field(default=0, ge=0)
    note: str | None = None


class DepositIn(BaseModel):
    amount: int = Field(gt=0)


class RepayIn(BaseModel):
    amount: int = Field(gt=0)
    # Also record it in an account (income when someone repays you, expense when you repay).
    account_id: str | None = None


class TontineIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    contribution: int = Field(gt=0, le=100_000_000)
    member_count: int = Field(ge=2, le=100)
    my_turn: int = Field(ge=1, le=100)
    start_date: date  # month of turn 1
    pay_day: int = Field(default=30, ge=1, le=31)


class TontinePayIn(BaseModel):
    account_id: str


class DebtIn(BaseModel):
    direction: Literal["lent", "borrowed"]
    person: str = Field(min_length=1, max_length=80)
    amount: int = Field(gt=0)
    note: str = ""
