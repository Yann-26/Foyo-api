from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    deadlines,
    documents,
    finances,
    health,
    households,
    invitations,
    menus,
    notifications,
    reports,
    subscriptions,
    tasks,
    telemetry,
)

api_router = APIRouter(prefix="/api/v1")
for module in (auth, households, invitations, notifications, tasks, reports, finances, deadlines, documents, health, menus, subscriptions, telemetry):
    api_router.include_router(module.router)
api_router.include_router(subscriptions.webhooks)
