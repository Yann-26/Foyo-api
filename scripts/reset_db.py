"""Drops every table, re-applies the migrations and loads the demo household (development only).

    .venv/bin/python -m scripts.reset_db
"""

import sys

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.infrastructure.db import models  # noqa: F401
from app.infrastructure.db.base import Base
from app.infrastructure.db.migrations import upgrade_to_head
from app.infrastructure.db.seed import seed

if __name__ == "__main__":
    if not get_settings().is_dev:
        sys.exit("Refusing to wipe the database outside ENVIRONMENT=development.")
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    upgrade_to_head()
    with SessionLocal() as db:
        seed(db)
    print("Database reset — log in with awa.kouassi@email.com / foyo1234")
