import os
import tempfile

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_foyo.db")
os.environ["ENVIRONMENT"] = "test"
os.environ["SEED_DEMO"] = "false"
os.environ["MEDIA_DIR"] = tempfile.mkdtemp(prefix="foyo-media-")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import SessionLocal, engine
from app.infrastructure.db import models  # noqa: F401
from app.infrastructure.db.base import Base
from app.infrastructure.db.migrations import upgrade_to_head
from app.infrastructure.db.seed import DEMO_PASSWORD, seed
from app.main import app


def _fresh_schema() -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    upgrade_to_head()  # the same migrations production runs


@pytest.fixture()
def client():
    _fresh_schema()
    with SessionLocal() as db:
        seed(db)
    with TestClient(app) as c:
        yield c
    _fresh_schema()


def login(client, email="awa.kouassi@email.com") -> dict:
    r = client.post("/api/v1/auth/sign-in", json={"email": email, "password": DEMO_PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def awa(client):
    return login(client)


@pytest.fixture()
def koffi(client):
    return login(client, "koffi.k@email.com")
