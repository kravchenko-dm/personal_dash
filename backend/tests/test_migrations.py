"""The Alembic chain must produce exactly the schema the models describe."""

import asyncio
import os

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.db import Base, alembic_config


def _diff_sync(conn):
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return compare_metadata(ctx, Base.metadata)


async def _schema_diff(url: str):
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff_sync)
    await engine.dispose()
    return diff


def test_migrations_match_models(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}"
    command.upgrade(alembic_config(url), "head")
    diff = asyncio.run(_schema_diff(url))
    assert diff == [], f"models and migrations diverged: {diff}"
    # downgrade to nothing and back up
    command.downgrade(alembic_config(url), "base")
    command.upgrade(alembic_config(url), "head")


def test_legacy_create_all_database_gets_stamped(tmp_path):
    """A DB created by the old create_all path must be adopted without errors."""
    from sqlalchemy import create_engine, inspect

    path = tmp_path / "legacy.db"
    # a pre-Alembic database looks like revision 0001 without the version table
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{path}"), "0001")
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE alembic_version")
    assert "alembic_version" not in inspect(sync_engine).get_table_names()
    sync_engine.dispose()

    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{path}"
    from app.config import get_settings

    get_settings.cache_clear()
    import app.db as dbmod

    original = dbmod.engine
    engine = create_async_engine(get_settings().database_url)
    dbmod.engine = engine
    try:
        asyncio.run(dbmod.init_db())
    finally:
        asyncio.run(engine.dispose())
        dbmod.engine = original
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
        get_settings.cache_clear()
    sync_engine = create_engine(f"sqlite:///{path}")
    names = inspect(sync_engine).get_table_names()
    assert (
        "alembic_version" in names and "login_events" in names and "media_jobs" in names
    )  # stamped 0001, upgraded to head
    with sync_engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0007"
    sync_engine.dispose()


def test_0007_moves_bot_guests_into_users(tmp_path):
    from sqlalchemy import create_engine

    path = tmp_path / "guests.db"
    url = f"sqlite+aiosqlite:///{path}"
    command.upgrade(alembic_config(url), "0006")
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO settings (key, value) VALUES ('telegram.guests', "
            '\'{"guests": [{"id": 200, "name": "Пётр", "username": "petr", "added_at": "x"}]}\')'
        )
        conn.exec_driver_sql("INSERT INTO tasks (title, status, priority, source, created_at, updated_at) "
                             "VALUES ('t', 'todo', 2, 'manual', '2026-01-01', '2026-01-01')")  # fmt: skip
    sync_engine.dispose()
    command.upgrade(alembic_config(url), "head")
    sync_engine = create_engine(f"sqlite:///{path}")
    with sync_engine.connect() as conn:
        users = conn.exec_driver_sql("SELECT id, telegram_id, name, role, level FROM users ORDER BY id").all()
        assert [tuple(u) for u in users] == [
            (1, None, "", "owner", "workspace"),
            (2, 200, "Пётр", "member", "media"),
        ]
        assert conn.exec_driver_sql("SELECT user_id FROM tasks").scalar() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM settings WHERE key='telegram.guests'").scalar() == 0
    sync_engine.dispose()
