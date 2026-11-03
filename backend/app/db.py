import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from alembic import command
from app.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url or get_settings().database_url)
    return cfg


def _inspect_sync(conn) -> tuple[set[str], bool]:
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    return tables, "alembic_version" in tables


async def init_db() -> None:
    """Bring the schema to the latest Alembic revision.

    Databases created before Alembic was introduced (plain create_all) have tables but no
    alembic_version: they are stamped at the initial revision and then upgraded."""
    from app import models  # noqa: F401  (register tables)

    async with engine.connect() as conn:
        tables, has_version = await conn.run_sync(_inspect_sync)
    cfg = alembic_config()
    if tables and not has_version:
        log.info("db: legacy schema without alembic_version, stamping 0001")
        await asyncio.to_thread(command.stamp, cfg, "0001")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    async with engine.connect() as conn:
        rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    log.info("db: schema at revision %s", rev)
