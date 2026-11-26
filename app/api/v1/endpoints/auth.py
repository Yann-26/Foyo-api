from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from app.api.deps import DB, CtxDep, CurrentUser
from app.application import account
from app.application.membership import place_new_account
from app.core import clock
from app.core.config import get_settings
from app.core.exceptions import bad_request, conflict
from app.core.security import (
    create_access_token,
    create_purpose_token,
    decode_purpose_token,
    hash_password,
    new_id,
    reset_code,
    verify_password,
)
from app.infrastructure.auth.social import SocialAuthError, SocialNotConfigured, verify_identity
from app.infrastructure.db.models import Member, PasswordReset, User
from app.infrastructure.email import email_client
from app.schemas.auth import DeleteAccountIn, ForgotPasswordIn, ResetPasswordIn, SignInIn, SignUpIn, SocialIn

router = APIRouter(prefix="/auth", tags=["auth"])



def _session(db, user: User) -> dict:
    member = db.scalars(select(Member).where(Member.user_id == user.id)).first()
    return {
        "access_token": create_access_token(user.id, user.token_version),
        "token_type": "bearer",
        "user": {
            "id": member.id if member else user.id,
            "first_name": user.first_name,
            "email": user.email,
            "email_verified": user.email_verified,
        },
        "household_id": member.household_id if member else None,
    }


@router.post("/sign-up", status_code=status.HTTP_201_CREATED)
def sign_up(body: SignUpIn, db: DB, background: BackgroundTasks) -> dict:
    if db.scalars(select(User).where(User.email == body.email)).first():
        raise conflict("Un compte existe déjà avec cet e-mail")
    user = User(id=new_id("u"), email=body.email, password_hash=hash_password(body.password), first_name=body.first_name)
    db.add(user)

    db.flush()
    place_new_account(db, user, body.invite_code)
    db.commit()
    _send_verification(background, user)
    return _session(db, user)


# ── E-mail verification (spec §5.1) ──


def _send_verification(background: BackgroundTasks, user: User) -> None:
    link = f"{get_settings().public_api_url.rstrip('/')}/api/v1/auth/verify-email?token={create_purpose_token(user.id, 'verify', user.email)}"
    background.add_task(
        email_client.deliver,
        user.email,
        "Foyo — confirmez votre adresse e-mail",
        f"Bonjour {user.first_name},\n\nConfirmez votre adresse en ouvrant ce lien (valable 7 jours) :\n{link}\n\n"
        "Si vous n’avez pas créé de compte Foyo, ignorez ce message.",
    )


_PAGE = """<!doctype html><html lang="fr"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Foyo</title><body style="font-family:sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem;text-align:center">
<h1 style="color:#1F6B4E">{title}</h1><p>{text}</p></body></html>"""


@router.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
def verify_email(token: str, db: DB) -> HTMLResponse:
    """The link in the e-mail, opened in a browser."""
    claims = decode_purpose_token(token, "verify")
    user = db.get(User, claims[0]) if claims else None
    if user is None or user.email != claims[1]:
        page = _PAGE.format(title="Lien expiré", text="Demandez un nouveau lien depuis l’application, dans Réglages.")
        return HTMLResponse(page, status_code=400)
    user.email_verified = True
    db.commit()
    return HTMLResponse(_PAGE.format(title="Adresse confirmée", text="Merci ! Vous pouvez retourner dans l’application Foyo."))


@router.post("/verify-email/resend", status_code=status.HTTP_202_ACCEPTED)
def resend_verification(ctx: CtxDep, background: BackgroundTasks) -> dict:
    if ctx.user.email_verified:
        return {"detail": "Adresse déjà confirmée"}
    _send_verification(background, ctx.user)
    return {"detail": f"Lien envoyé à {ctx.user.email}"}


# ── Google / Apple (spec §5.1) ──


@router.post("/social")
def social_sign_in(body: SocialIn, db: DB) -> dict:
    """Signs in (or up) with the ID token Google or Apple gave the app. An existing account with the same
    verified e-mail is linked rather than duplicated."""
    try:
        ident = verify_identity(body.provider, body.id_token)
    except SocialNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from e
    except SocialAuthError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e)) from e
    sub_col = User.google_sub if ident.provider == "google" else User.apple_sub
    user = db.scalars(select(User).where(sub_col == ident.sub)).first()
    if user is None and ident.email and ident.email_verified:
        user = db.scalars(select(User).where(User.email == ident.email)).first()
    if user is None:
        if not ident.email:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Adresse e-mail non transmise par le fournisseur")
        if db.scalars(select(User.id).where(User.email == ident.email)).first():
            # Same address, but the provider doesn't vouch for it: never take over an existing account.
            raise conflict("Un compte existe déjà avec cet e-mail : connectez-vous avec votre mot de passe")
        name = (body.first_name or ident.first_name or ident.email.split("@")[0]).strip()[:60]
        user = User(
            id=new_id("u"),
            email=ident.email,
            password_hash="!",  # no password: Google/Apple, or "mot de passe oublié" to create one
            first_name=name,
            email_verified=ident.email_verified,
        )
        db.add(user)
        db.flush()
        place_new_account(db, user, body.invite_code)
    setattr(user, f"{ident.provider}_sub", ident.sub)
    user.email_verified = user.email_verified or ident.email_verified
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    return _session(db, user)


# Compared against when the e-mail is unknown, so both cases take the same time.
_DUMMY_HASH = hash_password("timing-equaliser")


@router.post("/sign-in")
def sign_in(body: SignInIn, db: DB) -> dict:
    """After `LOGIN_MAX_FAILURES` wrong passwords the account is locked for `LOGIN_LOCK_MINUTES`."""
    s = get_settings()
    user = db.scalars(select(User).where(User.email == body.email.strip().lower())).first()
    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou mot de passe incorrect")
    now = clock.now()
    if user.locked_until and user.locked_until > now:
        minutes = max(1, round((user.locked_until - now).total_seconds() / 60))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Trop d’essais. Réessayez dans {minutes} min ou réinitialisez votre mot de passe.",
        )
    if not verify_password(body.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= s.login_max_failures:
            user.locked_until = now + timedelta(minutes=s.login_lock_minutes)
            user.failed_logins = 0
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "E-mail ou mot de passe incorrect")
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    return _session(db, user)


RESET_MAX_ATTEMPTS = 5
RESET_RESEND_SECONDS = 60
_RESET_SENT = {"detail": "Si un compte existe pour cette adresse, un code de réinitialisation vient d’être envoyé."}


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
def forgot_password(body: ForgotPasswordIn, db: DB, background: BackgroundTasks) -> dict:
    """Sends a 6-digit code. Same answer whether the account exists or not (no e-mail enumeration).
    The e-mail goes out after the response, so timing doesn't reveal it either and SMTP never blocks the app."""
    user = db.scalars(select(User).where(User.email == body.email.strip().lower())).first()
    if user is None:
        return _RESET_SENT
    now = clock.now()
    pending = list(db.scalars(select(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None))))
    if any(now - r.created_at < timedelta(seconds=RESET_RESEND_SECONDS) for r in pending):
        return _RESET_SENT  # throttle: one e-mail per minute
    for r in pending:
        r.used_at = now  # a new code replaces the previous ones
    code = reset_code()
    minutes = get_settings().password_reset_minutes
    db.add(
        PasswordReset(
            id=new_id("pr"),
            user_id=user.id,
            code_hash=hash_password(code),
            created_at=now,
            expires_at=now + timedelta(minutes=minutes),
        )
    )
    db.commit()
    background.add_task(
        email_client.deliver,
        user.email,
        "Foyo — votre code de réinitialisation",
        f"Bonjour {user.first_name},\n\nVotre code pour choisir un nouveau mot de passe : {code}\n"
        f"Il est valable {minutes} minutes. Si vous n’êtes pas à l’origine de cette demande, ignorez ce message.",
    )
    return _RESET_SENT


@router.post("/password/reset")
def reset_password(body: ResetPasswordIn, db: DB) -> dict:
    """Checks the code, sets the new password, signs out every other device and returns a new session."""
    invalid = bad_request("Code invalide ou expiré")
    user = db.scalars(select(User).where(User.email == body.email.strip().lower())).first()
    if user is None:
        raise invalid
    now = clock.now()
    reset = db.scalars(
        select(PasswordReset)
        .where(PasswordReset.user_id == user.id, PasswordReset.used_at.is_(None), PasswordReset.expires_at > now)
        .order_by(PasswordReset.created_at.desc())
    ).first()
    if reset is None or reset.attempts >= RESET_MAX_ATTEMPTS:
        raise invalid
    if not verify_password(body.code, reset.code_hash):
        reset.attempts += 1
        db.commit()
        left = RESET_MAX_ATTEMPTS - reset.attempts
        raise bad_request(f"Code incorrect. {left} essai(s) restant(s)." if left else "Code invalide ou expiré")
    reset.used_at = now
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    user.email_verified = True  # receiving the code proves the address
    user.failed_logins, user.locked_until = 0, None
    db.commit()
    return _session(db, user)


@router.get("/me")
def me(ctx: CtxDep) -> dict:
    from app.api.v1.endpoints.households import member_json

    return {
        "user": {
            "id": ctx.me.id,
            "first_name": ctx.user.first_name,
            "email": ctx.user.email,
            "email_verified": ctx.user.email_verified,
            "health_consent": ctx.user.health_consent_at is not None,
        },
        "member": member_json(ctx.me),
        "household_id": ctx.hid,
        "plan": ctx.household.plan,
    }


@router.get("/me/export")
def export_me(db: DB, user: CurrentUser) -> JSONResponse:
    """All personal data, as a JSON file (spec: data export)."""
    return JSONResponse(
        account.export_user(db, user),
        headers={"Content-Disposition": 'attachment; filename="foyo-mes-donnees.json"'},
    )


@router.post("/me/delete", status_code=status.HTTP_204_NO_CONTENT)
def delete_me(body: DeleteAccountIn, db: DB, user: CurrentUser) -> None:
    if body.confirm.strip().upper() != "SUPPRIMER":
        raise bad_request("Tapez SUPPRIMER pour confirmer.")
    if user.password_hash != "!" and not verify_password(body.password or "", user.password_hash):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Mot de passe incorrect.")
    account.delete_user(db, user)
