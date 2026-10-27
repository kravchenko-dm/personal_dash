from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.models import TelegramChat
from app.schemas import TelegramChatOut
from app.services import telegram_bot

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


@router.post("/webhook", include_in_schema=False)
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != get_settings().telegram_webhook_secret:
        raise HTTPException(403, "bad secret")
    await telegram_bot.feed_update(await request.json())
    return {"ok": True}


@router.get("/chats", response_model=list[TelegramChatOut], dependencies=[Depends(require_auth)])
async def chats(db: AsyncSession = Depends(get_db)):
    return list((await db.scalars(select(TelegramChat).order_by(TelegramChat.title))).all())


@router.patch("/chats/{chat_id}", response_model=TelegramChatOut, dependencies=[Depends(require_auth)])
async def toggle_chat(chat_id: int, collect: bool, db: AsyncSession = Depends(get_db)):
    chat = await db.get(TelegramChat, chat_id)
    if not chat:
        raise HTTPException(404, "Not found")
    chat.collect = collect
    await db.commit()
    await db.refresh(chat)
    return chat
