from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from gl_api.config import MigrationSettings, load_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = load_settings(MigrationSettings)
config.attributes["gl_runtime_role"] = settings.application_role
config.attributes["gl_migrator_role"] = settings.database_user
url = settings.database_url().replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    raise RuntimeError("offline migrations are disabled")


def run_migrations_online() -> None:
    engine = create_engine(
        url, poolclass=pool.NullPool, connect_args=settings.migration_connect_args()
    )
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, transactional_ddl=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
