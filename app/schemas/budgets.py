from pydantic import BaseModel, Field


class BudgetIn(BaseModel):
    category_id: str
    planned: int = Field(ge=0)
    month: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")
