import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.idempotency import IdempotencyMiddleware
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.migrations import upgrade_to_head
from app.infrastructure.email.email_client import EmailConfigError
from app.infrastructure.email.email_client import check_config as check_email_config
from app.infrastructure.notifications.fcm_client import PushConfigError
from app.infrastructure.notifications.fcm_client import check_config as check_push_config


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    try:
        check_email_config()
    except EmailConfigError as e:
        logging.getLogger("foyo").error("E-mail disabled: %s", e)
    try:
        check_push_config()
    except PushConfigError as e:
        logging.getLogger("foyo").error("Push notifications disabled: %s", e)
    settings = get_settings()
    if settings.is_dev:
        # Production applies migrations before starting (docker/entrypoint.sh), once, not per worker.
        upgrade_to_head()
    _starter_dishes()
    if settings.seed_demo:
        from app.infrastructure.db.seed import seed_if_empty

        seed_if_empty()
    yield


def _starter_dishes() -> None:
    """The shared catalogue of 30 dishes, in every installation (not only the demo)."""
    from app.application.starter_dishes import ensure_starter_dishes
    from app.core.database import SessionLocal

    try:
        with SessionLocal() as db:
            ensure_starter_dishes(db)
    except Exception:
        logging.getLogger("foyo").exception("Starter dishes not loaded")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(IdempotencyMiddleware)
    app.include_router(api_router)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
