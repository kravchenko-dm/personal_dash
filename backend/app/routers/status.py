from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.schemas import StatusOut
from app.services import calendar, icloud, telegram_bot
from app.services.bot_access import Account
from app.services.llm import describe as describe_llm

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/status", response_model=StatusOut)
async def status(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    s = get_settings()
    llm = describe_llm(s)
    if not user.is_owner:  # members see only what their own pages need
        return StatusOut(
            push=s.push_enabled, telegram=s.telegram_enabled, claude=llm["enabled"], llm_provider="",
            llm_model="", google=False, google_connected=False, yougile=False, icloud=False,
            vapid_public_key=s.vapid_public_key, bot_username=telegram_bot.bot_username(),
        )  # fmt: skip
    return StatusOut(
        push=s.push_enabled,
        telegram=s.telegram_enabled,
        claude=llm["enabled"],
        llm_provider=llm["provider"],
        llm_model=llm["model"],
        google=s.google_enabled,
        google_connected=await calendar.is_connected(db),
        yougile=bool(s.yougile_api_key),
        icloud=icloud.enabled(),
        vapid_public_key=s.vapid_public_key,
        bot_username=telegram_bot.bot_username(),
    )
