"""Alembic environment: uses DATABASE_URL from app settings and the ORM metadata."""

from alembic import context
from app.core.database import engine
from app.infrastructure.db import models  # noqa: F401  (registers tables)
from app.infrastructure.db.base import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=str(engine.url), target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:  # called from app code with an open connection
        _run(connection)
        return
    with engine.connect() as conn:
        _run(conn)


def _run(conn) -> None:  # noqa: ANN001
    # render_as_batch: SQLite can't ALTER most things, batch mode rebuilds the table instead.
    # Rebuilding = copy, DROP, rename: with foreign keys enforced, the DROP would fire ON DELETE
    # SET NULL / CASCADE on every referencing row (members would lose their account). So foreign
    # keys are switched off while migrating on SQLite (the PRAGMA only works outside a transaction).
    sqlite = conn.dialect.name == "sqlite"
    if sqlite:
        if conn.in_transaction():
            conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if sqlite:
            if conn.in_transaction():
                conn.commit()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
