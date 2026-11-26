"""`Idempotency-Key` support: an action replayed by the app (made offline, or retried after a dropped
connection) returns the answer of its first run instead of being applied twice."""

from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core import clock
from app.core.database import SessionLocal
from app.core.security import decode_access_token, new_id
from app.infrastructure.db.models import IdempotencyKey

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
KEEP = timedelta(days=30)


def _user(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    claims = decode_access_token(auth[7:])
    return claims[0] if claims else None


def _find(user_id: str, key: str) -> IdempotencyKey | None:
    with SessionLocal() as db:
        return db.scalars(select(IdempotencyKey).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)).first()


def _store(user_id: str, key: str, request: Request, status: int, body: bytes) -> None:
    with SessionLocal() as db:
        db.add(
            IdempotencyKey(
                id=new_id("ik"), user_id=user_id, key=key, method=request.method, path=request.url.path,
                status_code=status, body=body.decode("utf-8", "replace"), created_at=clock.now(),
            )
        )
        try:
            db.commit()
        except IntegrityError:  # the same key arrived twice at once: the first answer stands
            db.rollback()


def purge_old_keys(db, now) -> int:  # noqa: ANN001
    """Worker job: forget answers older than 30 days."""
    n = db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < now - KEEP)).rowcount
    db.commit()
    return n or 0


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        key = request.headers.get("idempotency-key")
        if request.method not in MUTATING or not key or len(key) > 80:
            return await call_next(request)
        user_id = _user(request)
        if user_id is None:
            return await call_next(request)
        done = await run_in_threadpool(_find, user_id, key)
        if done is not None:
            return Response(
                content=done.body, status_code=done.status_code, media_type="application/json",
                headers={"Idempotent-Replay": "true"},
            )
        response = await call_next(request)
        body = b"".join([chunk async for chunk in response.body_iterator])
        if response.status_code < 500:  # a server error may succeed on the next try: don't remember it
            await run_in_threadpool(_store, user_id, key, request, response.status_code, body)
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=body, status_code=response.status_code, headers=headers, media_type=response.media_type)
