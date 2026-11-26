"""Run Alembic from code (development start-up, reset script, tests)."""

from pathlib import Path

from alembic.config import Config

from alembic import command

_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def alembic_config() -> Config:
    cfg = Config(str(_INI))
    cfg.set_main_option("script_location", str(_INI.parent / "alembic"))
    return cfg


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")
