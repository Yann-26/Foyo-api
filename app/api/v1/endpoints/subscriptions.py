import hmac
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, status

from app.api.deps import DB, CtxDep
from app.application.premium import FREE_LIMITS, TRIAL_DAYS, premium_active, usage
from app.core import clock
from app.core.config import get_settings
from app.core.exceptions import bad_request
from app.infrastructure.db.models import Household
from app.schemas.subscriptions import PlanChangeIn

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])

# Prices are hypotheses to be tested (spec §9). Store purchases go through RevenueCat.
PLANS = [
    {
        "plan": "tasks",
        "description": "Rappels, responsables, chrono, menus, tableaux à imprimer",
        "free_tier": "Gratuit · Premium 1 500 FCFA / mois",
        "monthly": 1500,
        "yearly": 12000,
        "recommended": False,
    },
    {
        "plan": "finances",
        "description": "Entrées, dépenses, budgets, épargne, tontines",
        "free_tier": "Gratuit · Premium 1 500 FCFA / mois",
        "monthly": 1500,
        "yearly": 12000,
        "recommended": False,
    },
    {
        "plan": "complete",
        "description": "Tâches et finances reliées : le marché devient une dépense, la facture se règle en un geste",
        "free_tier": "Gratuit · Premium 2 500 FCFA / mois",
        "monthly": 2500,
        "yearly": 20000,
        "recommended": True,
    },
]


@router.get("/plans")
def plans() -> list[dict]:
    return PLANS


def _current(ctx: CtxDep) -> dict:
    hh = ctx.household
    return {
        "plan": hh.plan,
        "is_premium": premium_active(hh),
        "premium_until": hh.premium_until.isoformat(timespec="minutes") if hh.premium_until else None,
        "premium_source": hh.premium_source,
        "premium_period": hh.premium_period,
        "will_renew": hh.premium_will_renew,
        "trial_used": hh.trial_used,
        "trial_days": TRIAL_DAYS,
        # The RevenueCat "app user id": purchases are attached to the household, not to one member.
        "billing_id": hh.id,
        "limits": FREE_LIMITS,
        "usage": usage(ctx.db, hh),
    }


@router.get("/current")
def current(ctx: CtxDep) -> dict:
    return _current(ctx)


@router.put("/current")
def change_plan(body: PlanChangeIn, ctx: CtxDep) -> dict:
    """Switching never deletes data: the other module's data is kept 12 months (spec §5.12)."""
    ctx.require_admin()
    ctx.household.plan = body.plan
    ctx.db.commit()
    return _current(ctx)


@router.post("/trial")
def start_trial(ctx: CtxDep) -> dict:
    """7 days of Premium, once per household (spec F12)."""
    ctx.require_admin()
    hh = ctx.household
    if hh.trial_used:
        raise bad_request("L’essai gratuit a déjà été utilisé par ce foyer")
    if premium_active(hh):
        raise bad_request("Le foyer est déjà Premium")
    hh.trial_used = True
    hh.premium_until = clock.now() + timedelta(days=TRIAL_DAYS)
    hh.premium_source, hh.premium_period, hh.premium_will_renew = "trial", None, False
    ctx.db.commit()
    return _current(ctx)


# ── RevenueCat (store purchases) ──

webhooks = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Store product ids look like "foyo_complete_yearly" / "foyo.tasks.monthly".
_ACTIVE = {"INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE", "UNCANCELLATION", "NON_RENEWING_PURCHASE", "SUBSCRIPTION_EXTENDED"}


def _product(product_id: str) -> tuple[str | None, str | None]:
    pid = product_id.lower()
    plan = next((p for p in ("complete", "tasks", "finances") if p in pid), None)
    period = "yearly" if ("year" in pid or "annual" in pid) else "monthly" if "month" in pid else None
    return plan, period


@webhooks.post("/revenuecat")
def revenuecat(body: dict, db: DB, authorization: str | None = Header(default=None)) -> dict:
    """Purchases, renewals, cancellations and expiries sent by RevenueCat (app user id = household id)."""
    secret = get_settings().revenuecat_webhook_secret
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook RevenueCat non configuré")
    if not authorization or not hmac.compare_digest(authorization, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Signature invalide")
    event = body.get("event") or {}
    kind = event.get("type")
    hh = db.get(Household, event.get("app_user_id") or "")
    if kind == "TEST" or hh is None:
        return {"ok": True, "applied": False}
    expires = event.get("expiration_at_ms")
    until = clock.to_local(datetime.fromtimestamp(expires / 1000, UTC)) if expires else None
    if kind in _ACTIVE:
        plan, period = _product(event.get("product_id") or "")
        hh.premium_until = until or clock.now() + timedelta(days=31)
        hh.premium_source, hh.premium_will_renew = "store", kind != "NON_RENEWING_PURCHASE"
        hh.premium_period = period or hh.premium_period
        if plan:
            hh.plan = plan  # the formula bought is the household's formula
    elif kind == "CANCELLATION":
        hh.premium_will_renew = False  # still Premium until the end of the paid period
    elif kind == "EXPIRATION":
        hh.premium_until = min(filter(None, [hh.premium_until, until, clock.now()]))
        hh.premium_will_renew = False
    db.commit()
    return {"ok": True, "applied": True}
