import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: str, version: int = 0) -> str:
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(days=s.access_token_days)
    return jwt.encode({"sub": user_id, "ver": version, "exp": exp}, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> tuple[str, int] | None:
    """(user id, token version) or None when invalid / expired."""
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    return claims.get("sub"), int(claims.get("ver", 0))


def create_purpose_token(user_id: str, purpose: str, email: str, days: int = 7) -> str:
    """Signed link token (e.g. e-mail verification): only valid for that purpose and that address."""
    s = get_settings()
    exp = datetime.now(UTC) + timedelta(days=days)
    return jwt.encode({"sub": user_id, "purpose": purpose, "email": email, "exp": exp}, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_purpose_token(token: str, purpose: str) -> tuple[str, str] | None:
    s = get_settings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if claims.get("purpose") != purpose:
        return None
    return claims.get("sub"), claims.get("email")


def reset_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def invite_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"
