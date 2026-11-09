import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # Only manage our own tables: the PostGIS image also ships tiger/topology tables that
    # Alembic must never try to drop.
    if reflected and compare_to is None:
        return False
    return not (type_ == "index" and obj.table.name not in target_metadata.tables)


def url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url


def run_offline() -> None:
    context.configure(
        url=url(), target_metadata=target_metadata, literal_binds=True, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
