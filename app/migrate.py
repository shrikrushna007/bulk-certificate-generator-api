from __future__ import annotations

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.config import Settings

_MIGRATION_LOCK_ID = 1_093_691_172


def escape_alembic_config_value(value: str) -> str:
    """Escape percent signs interpreted by Alembic's INI parser."""
    return value.replace("%", "%%")


def upgrade_database() -> None:
    """Upgrade the database while serializing PostgreSQL migrators."""
    settings = Settings()
    alembic_config = Config("alembic.ini")
    if settings.database_url.startswith("postgresql"):
        engine = create_engine(settings.database_url, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"),
                {"lock_id": _MIGRATION_LOCK_ID},
            )
            try:
                command.upgrade(alembic_config, "head")
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": _MIGRATION_LOCK_ID},
                )
        engine.dispose()
        return

    command.upgrade(alembic_config, "head")


if __name__ == "__main__":
    upgrade_database()
