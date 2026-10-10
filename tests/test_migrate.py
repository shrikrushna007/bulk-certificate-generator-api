from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.migrate import escape_alembic_config_value


def test_encoded_database_password_survives_alembic_interpolation() -> None:
    database_url = "postgresql+psycopg://user:p%40ss%25word@db.example/test"
    config = Config()

    config.set_main_option("sqlalchemy.url", escape_alembic_config_value(database_url))

    assert config.get_main_option("sqlalchemy.url") == database_url


def test_migrations_upgrade_empty_database_to_head(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "migrated.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        schema = inspect(engine)
        assert set(schema.get_table_names()) >= {
            "alembic_version",
            "certificate_jobs",
            "certificate_records",
        }
        job_columns = {column["name"] for column in schema.get_columns("certificate_jobs")}
        assert {
            "archive_total_count",
            "archive_processed_count",
            "archive_started_at",
            "archive_completed_at",
        } <= job_columns
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "20261010_05"
    finally:
        engine.dispose()
