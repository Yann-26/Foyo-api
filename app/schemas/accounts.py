from typing import Literal

from pydantic import BaseModel, Field

AccountType = Literal["cash", "wave", "orangeMoney", "mtnMomo", "moov", "bank", "other"]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    type: AccountType = "cash"
    balance: int = 0
    is_private: bool = False
