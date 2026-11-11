import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.routers import (
    agent,
    apple,
    auth,
    backups,
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
from app.services import scheduler, security, telegram_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# chatty third-party loggers: one line per job run / HTTP request / Apple icalendar quirk
for _name, _level in (
    ("apscheduler", logging.WARNING),
    ("httpx", logging.WARNING),
    ("httpcore", logging.WARNING),
    ("google_auth_httplib2", logging.WARNING),
    ("googleapiclient", logging.WARNING),
    ("caldav", logging.ERROR),  # rate-limited "icalendar data breaks the standard" diffs
):
    logging.getLogger(_name).setLevel(_level)
log = logging.getLogger(__name__)

_TOKEN_IN_URL = re.compile(r"((?:^|[?&])token=)[^&\s]+")


class _RedactTokens(logging.Filter):
    """Access log lines carry the full URL: hide ?token=… (Apple Shortcuts, agent installer)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            args = list(record.args)
            args[2] = _TOKEN_IN_URL.sub(r"\1***", args[2])
            record.args = tuple(args)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokens())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with SessionLocal() as db:
        await security.load_epoch(db)
    try:
        await telegram_bot.setup()
    except Exception as exc:
        log.exception("telegram setup failed: %s", exc)
    scheduler.start()
    yield
    scheduler.stop()
    await telegram_bot.shutdown()


_LOCAL = get_settings().app_domain in ("localhost", "127.0.0.1")
# the API map is not published in production
app = FastAPI(
    title="Personal Dash",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _LOCAL else None,
    redoc_url="/redoc" if _LOCAL else None,
    openapi_url="/openapi.json" if _LOCAL else None,
)

if _LOCAL:
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
    backups,
):
    app.include_router(r.router)
