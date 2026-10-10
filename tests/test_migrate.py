from alembic.config import Config

from app.migrate import escape_alembic_config_value


def test_encoded_database_password_survives_alembic_interpolation() -> None:
    database_url = "postgresql+psycopg://user:p%40ss%25word@db.example/test"
    config = Config()

    config.set_main_option(
        "sqlalchemy.url", escape_alembic_config_value(database_url)
    )

    assert config.get_main_option("sqlalchemy.url") == database_url
