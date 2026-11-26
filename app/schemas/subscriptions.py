from typing import Literal

from pydantic import BaseModel


class PlanChangeIn(BaseModel):
    plan: Literal["tasks", "finances", "complete"]
