import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import init_db
from app.routers import (
    agent,
    apple,
    auth,
    balances,
    calendar,
    monitoring,
    projects,
    push,
    status,
    summaries,
    tasks,
    telegram,
    yougile,
)
from app.services import scheduler, telegram_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:
        await telegram_bot.setup()
    except Exception as exc:
        log.exception("telegram setup failed: %s", exc)
    scheduler.start()
    yield
    scheduler.stop()
    await telegram_bot.shutdown()


app = FastAPI(title="Personal Dash", version="0.1.0", lifespan=lifespan)

if get_settings().app_domain in ("localhost", "127.0.0.1"):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

for r in (
    status,
    auth,
    tasks,
    balances,
    push,
    telegram,
    summaries,
    calendar,
    yougile,
    apple,
    projects,
    monitoring,
    agent,
):
    app.include_router(r.router)
