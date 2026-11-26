"""Google and Apple sign-in: the app sends the ID token the provider gave it; the server checks it
with the provider's public keys (never trusts what the app says about the user)."""

from dataclasses import dataclass
from functools import lru_cache

import jwt

from app.core.config import get_settings

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS = "https://appleid.apple.com/auth/keys"


class SocialAuthError(Exception):
    pass


class SocialNotConfigured(SocialAuthError):
    pass


@dataclass
class SocialIdentity:
    provider: str
    sub: str  # stable user id at the provider
    email: str | None
    email_verified: bool
    first_name: str | None


def _google(token: str) -> SocialIdentity:
    audiences = get_settings().google_client_ids
    if not audiences:
        raise SocialNotConfigured("Connexion Google non configurée sur ce serveur")
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    try:
        info = id_token.verify_oauth2_token(token, google_requests.Request(), audience=audiences)
    except ValueError as e:
        raise SocialAuthError("Jeton Google invalide") from e
    return SocialIdentity(
        provider="google",
        sub=info["sub"],
        email=(info.get("email") or "").lower() or None,
        email_verified=bool(info.get("email_verified")),
        first_name=info.get("given_name") or info.get("name"),
    )


@lru_cache
def _apple_keys() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(APPLE_KEYS, cache_keys=True)


def _apple(token: str) -> SocialIdentity:
    audiences = get_settings().apple_audiences
    if not audiences:
        raise SocialNotConfigured("Connexion Apple non configurée sur ce serveur")
    try:
        key = _apple_keys().get_signing_key_from_jwt(token).key
        info = jwt.decode(token, key, algorithms=["RS256"], audience=audiences, issuer=APPLE_ISSUER)
    except jwt.PyJWTError as e:
        raise SocialAuthError("Jeton Apple invalide") from e
    verified = info.get("email_verified")
    return SocialIdentity(
        provider="apple",
        sub=info["sub"],
        email=(info.get("email") or "").lower() or None,
        email_verified=verified is True or verified == "true",
        first_name=None,  # Apple only sends the name to the app, the first time
    )


def verify_identity(provider: str, token: str) -> SocialIdentity:
    match provider:
        case "google":
            return _google(token)
        case "apple":
            return _apple(token)
    raise SocialAuthError("Fournisseur inconnu")
