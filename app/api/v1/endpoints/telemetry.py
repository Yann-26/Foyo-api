"""Anonymous usage statistics (opt-in) and crash reports from the app. No account needed: a crash can
happen before sign-in. Nothing identifies the person: a random id per installation."""

import hashlib
import re
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DB
from app.core import clock
from app.core.config import get_settings
from app.core.rate_limit import SlidingWindowLimiter, retry_seconds
from app.core.security import new_id
from app.infrastructure.db.models import CrashReport, TelemetryEvent
from app.schemas.telemetry import CrashIn, EventsIn

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

_s = get_settings()
# Module-level: the counts live as long as the API process.
LIMITS = {
    "events_ip": SlidingWindowLimiter(_s.telemetry_ip_requests_per_minute, 60),
    "events_install": SlidingWindowLimiter(_s.telemetry_install_requests_per_minute, 60),
    "events_volume": SlidingWindowLimiter(_s.telemetry_install_events_per_hour, 3600),
    "crashes_ip": SlidingWindowLimiter(_s.telemetry_ip_crashes_per_minute, 60),
    "crashes_install": SlidingWindowLimiter(_s.telemetry_install_crashes_per_hour, 3600),
}


def _client_ip(request: Request) -> str:
    """The client's address. Behind a reverse proxy, uvicorn takes it from X-Forwarded-For only when the
    proxy is listed in FORWARDED_ALLOW_IPS (docker/entrypoint.sh), so it can't be faked."""
    return request.client.host if request.client else "unknown"


def _limit(*checks: tuple[str, str, int]) -> None:
    """Each check is (limiter name, key, cost), in order; a refused request still counts against the
    limits checked before it."""
    for name, key, cost in checks:
        wait = LIMITS[name].hit(key, cost)
        if wait is not None:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Trop d’envois, réessayez plus tard.",
                headers={"Retry-After": str(retry_seconds(wait))},
            )


_NOISE = re.compile(r"0x[0-9a-f]+|\b\d+\b|#\d+\s+")


def fingerprint(error: str, stack: str) -> str:
    """The error type and the top app frames, without addresses or line-specific numbers."""
    frames = [line for line in stack.splitlines() if "package:foyo/" in line][:5]
    head = error.split(":", 1)[0]
    return hashlib.sha256(_NOISE.sub("", head + "\n" + "\n".join(frames)).encode()).hexdigest()


def _per_ip(name: str) -> Callable[[Request], None]:
    """As a dependency, the per-IP limit runs even when the body is invalid (floods of bad requests)."""

    def check(request: Request) -> None:
        _limit((name, _client_ip(request), 1))

    return check


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
def events(body: EventsIn, db: DB, _: Annotated[None, Depends(_per_ip("events_ip"))]) -> None:
    _limit(
        ("events_install", body.install_id, 1),
        ("events_volume", body.install_id, len(body.events)),
    )
    now = clock.now()
    for e in body.events:
        at = clock.to_local(e.at)
        db.add(
            TelemetryEvent(
                id=new_id("ev"),
                install_id=body.install_id,
                name=e.name,
                props=e.props,
                app_version=body.app_version,
                platform=body.platform,
                at=min(at, now),  # a wrong phone clock can't write the future
            )
        )
    db.commit()


@router.post("/crashes", status_code=status.HTTP_204_NO_CONTENT)
def crash(body: CrashIn, db: DB, _: Annotated[None, Depends(_per_ip("crashes_ip"))]) -> None:
    _limit(("crashes_install", body.install_id, 1))
    db.add(
        CrashReport(
            id=new_id("cr"),
            install_id=body.install_id,
            app_version=body.app_version,
            platform=body.platform,
            error=body.error[:500],
            stack=body.stack,
            fingerprint=fingerprint(body.error, body.stack),
            at=min(clock.to_local(body.at), clock.now()) if body.at else clock.now(),
        )
    )
    db.commit()
