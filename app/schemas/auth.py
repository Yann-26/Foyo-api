from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SignUpIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    invite_code: str | None = Field(default=None, max_length=20)  # from a shared link

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Adresse e-mail invalide")
        return v


class SocialIn(BaseModel):
    provider: Literal["google", "apple"]
    id_token: str = Field(min_length=20, max_length=8192)
    first_name: str | None = Field(default=None, max_length=60)  # Apple gives it to the app, once
    invite_code: str | None = Field(default=None, max_length=20)


class SignInIn(BaseModel):
    email: str
    password: str


class ForgotPasswordIn(BaseModel):
    email: str = Field(max_length=255)


class ResetPasswordIn(BaseModel):
    email: str = Field(max_length=255)
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=8, max_length=128)


class DeleteAccountIn(BaseModel):
    # Typed by the person, so a mis-tap can't erase an account.
    confirm: str = Field(max_length=20)
    # Required for accounts with a password (not for Google / Apple-only accounts).
    password: str | None = Field(default=None, max_length=128)
