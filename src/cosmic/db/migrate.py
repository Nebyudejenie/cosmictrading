"""Programmatic Alembic configuration (works from any working directory)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from cosmic.core.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(settings: Settings, database: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["dsn"] = settings.owner_dsn(database)
    cfg.attributes["app_role"] = settings.db_app_user
    return cfg


def upgrade(settings: Settings, revision: str = "head", database: str | None = None) -> None:
    command.upgrade(alembic_config(settings, database), revision)


def downgrade(settings: Settings, revision: str, database: str | None = None) -> None:
    command.downgrade(alembic_config(settings, database), revision)


def head_revision() -> str:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if head is None:
        raise RuntimeError("no migrations found")
    return head
