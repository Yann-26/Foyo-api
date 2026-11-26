from datetime import date as date_
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Origin = Literal["manual", "task", "deadline", "shoppingList", "tontine", "recurring", "debt", "fee"]


class OperationIn(BaseModel):
    type: Literal["expense", "income", "transfer"]
    amount: int = Field(gt=0, le=1_000_000_000)
    category_id: str = Field(min_length=1, max_length=30)
    account_id: str
    to_account_id: str | None = None
    date: datetime | None = None
    author_id: str | None = None
    label: str | None = Field(default=None, max_length=120)
    origin: Origin = "manual"
    # Mobile-money or bank fee paid on top, from the same account: a separate "frais" expense.
    fee: int = Field(default=0, ge=0, le=10_000_000)
    # Also repeat it every N months from the next due date (1 monthly, 3 quarterly, 12 yearly).
    repeat_every_months: Literal[1, 3, 12] | None = None


class RecurringIn(BaseModel):
    type: Literal["expense", "income", "transfer"]
    amount: int = Field(gt=0, le=1_000_000_000)
    fee: int = Field(default=0, ge=0, le=10_000_000)
    category_id: str = Field(min_length=1, max_length=30)
    account_id: str
    to_account_id: str | None = None
    label: str | None = Field(default=None, max_length=120)
    every_months: Literal[1, 3, 12] = 1
    day: int = Field(ge=1, le=31)
    start: date_ | None = None  # first due date (default: next occurrence of `day`)


class RecurringPatchIn(BaseModel):
    amount: int | None = Field(default=None, gt=0, le=1_000_000_000)
    fee: int | None = Field(default=None, ge=0, le=10_000_000)
    label: str | None = Field(default=None, max_length=120)
    day: int | None = Field(default=None, ge=1, le=31)
    active: bool | None = None
