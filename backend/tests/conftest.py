import os

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["DASH_PASSWORD"] = "secret"
os.environ["APP_SECRET"] = "test-secret-that-is-long-enough-for-hs256-0000"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def _fresh_db():
    from app import models  # noqa: F401
    from app.db import SessionLocal
    from app.services import bot_access

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    bot_access._requested.clear()
    async with SessionLocal() as db:
        await bot_access.load(db)  # creates the owner (user 1) like migration 0007 does
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth(client: AsyncClient) -> dict[str, str]:
    resp = await client.post("/api/auth/login", json={"password": "secret"})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['token']}"}


MEMBER_TG = 555


@pytest_asyncio.fixture
async def member() -> int:
    """A member with a workspace; returns their users.id."""
    from app.db import SessionLocal
    from app.services import bot_access

    async with SessionLocal() as db:
        return (await bot_access.add(db, MEMBER_TG, "Пётр", "petr", level="workspace")).id


@pytest_asyncio.fixture
async def member_auth(member: int) -> dict[str, str]:
    from app.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(member)}"}


@pytest.fixture(scope="session", autouse=True)
def _cleanup():
    yield
    if os.path.exists("test.db"):
        os.remove("test.db")
