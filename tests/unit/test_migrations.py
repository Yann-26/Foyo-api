"""The migrations must build exactly the schema the models describe."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from app.infrastructure.db.base import Base
from app.infrastructure.db.migrations import alembic_config


def test_upgrade_matches_models_and_downgrade_cleans_up(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    eng = create_engine(url)
    monkeypatch.setattr("app.core.database.engine", eng)
    cfg = alembic_config()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"models and migrations differ: {diff} — run `alembic revision --autogenerate`"
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert set(inspect(eng).get_table_names()) <= {"alembic_version"}


def test_upgrading_a_populated_sqlite_db_keeps_memberships(tmp_path, monkeypatch):
    """Regression: SQLite batch migrations rebuild tables; with foreign keys on, rebuilding `users`
    used to fire ON DELETE SET NULL and detach every member from their account."""
    from sqlalchemy import event, text

    url = f"sqlite:///{tmp_path / 'p.db'}"
    eng = create_engine(url)

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _):  # same as the app engine
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr("app.core.database.engine", eng)
    cfg = alembic_config()
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "4d42f7ccecf9")  # initial schema, before the users table changed
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (id, email, password_hash, first_name, email_verified, token_version,"
            " failed_logins) VALUES ('u1', 'a@x.test', 'h', 'A', 1, 0, 0)"
        ))
        conn.execute(text(
            "INSERT INTO households (id, name, invite_code, plan, is_premium, categories, onboarding_completed)"
            " VALUES ('h1', 'H', 'C', 'complete', 0, '[]', 1)"
        ))
        conn.execute(text(
            "INSERT INTO members (id, household_id, user_id, first_name, role, color, points, active)"
            " VALUES ('m1', 'h1', 'u1', 'A', 'admin', 4281236823, 0, 1)"
        ))
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT user_id FROM members WHERE id = 'm1'")).scalar() == "u1"
        assert conn.execute(text("SELECT push_enabled FROM users WHERE id = 'u1'")).scalar() == 1
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1  # switched back on
