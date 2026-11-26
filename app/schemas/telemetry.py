from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_Value = str | int | float | bool | None


class _Install(BaseModel):
    install_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,40}$")
    app_version: str = Field(max_length=20)
    platform: Literal["android", "ios", "other"]


class EventIn(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,39}$")
    at: datetime
    props: dict[str, _Value] = Field(default_factory=dict)

    @field_validator("props")
    @classmethod
    def _small(cls, v: dict) -> dict:
        if len(v) > 10:
            raise ValueError("10 propriétés maximum")
        return {k[:30]: (x[:80] if isinstance(x, str) else x) for k, x in v.items()}


class EventsIn(_Install):
    events: list[EventIn] = Field(min_length=1, max_length=100)


class CrashIn(_Install):
    error: str = Field(max_length=2000)
    stack: str = Field(default="", max_length=20000)
    at: datetime | None = None
