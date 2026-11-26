from datetime import date as date_
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RecurrenceIn(BaseModel):
    kind: Literal["once", "daily", "weekly", "monthly", "everyNDays", "custom"] = "daily"
    weekdays: list[int] = []
    interval: int = Field(default=1, ge=1, le=365)

    @field_validator("weekdays")
    @classmethod
    def _days(cls, v: list[int]) -> list[int]:
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("Jours de 1 (lundi) à 7 (dimanche)")
        return sorted(set(v))


class SubStepIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)


class TaskIn(BaseModel):
    """Validation rules from spec §13: name ≤ 60 chars, ≤ 10 sub-steps, 1 min – 8 h."""

    name: str = Field(min_length=1, max_length=60)
    category_id: str
    recurrence: RecurrenceIn = RecurrenceIn()
    estimated_minutes: int = Field(ge=1, le=480)
    assignee_id: str | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    rotation: bool = False
    reminder_offset: int = Field(default=0, ge=0, le=1440)
    sub_steps: list[SubStepIn] = Field(default=[], max_length=10)
    detail: str | None = Field(default=None, max_length=120)


class TaskPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    estimated_minutes: int | None = Field(default=None, ge=1, le=480)
    assignee_id: str | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    rotation: bool | None = None
    recurrence: RecurrenceIn | None = None
    detail: str | None = Field(default=None, max_length=120)


class CompleteIn(BaseModel):
    # None = ticked without the timer ("Fait" from a notification): the estimate is used.
    actual_seconds: int | None = Field(default=None, ge=0, le=24 * 3600)
    # Sent by the app when the task was ticked offline and the action is replayed later.
    completed_at: datetime | None = None
    date: date_ | None = None  # day of the occurrence (defaults to today)
    note: str | None = Field(default=None, max_length=200)


class StartIn(BaseModel):
    started_at: datetime | None = None
    date: date_ | None = None


class OccurrencePatchIn(BaseModel):
    sub_steps_done: list[bool] | None = None
    status: Literal["todo", "postponed", "missed"] | None = None
    note: str | None = Field(default=None, max_length=200)
