from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.models import MediaJob, TelegramChat
from app.schemas import TelegramChatOut
from app.services import bot_access, telegram_bot

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


class GuestIn(BaseModel):
    id: int = Field(gt=0)
    name: str = ""


@router.get("/guests", dependencies=[Depends(require_auth)])
async def list_guests(db: AsyncSession = Depends(get_db)):
    """Bot users besides the owner, with how many recordings they sent and what it cost."""
    usage = {
        row.chat_id: (row.n, row.cost)
        for row in await db.execute(
            select(
                MediaJob.chat_id,
                func.count().label("n"),
                func.coalesce(func.sum(MediaJob.cost_rub), 0).label("cost"),
            ).group_by(MediaJob.chat_id)
        )
    }
    return {
        "guests": [
            {
                **g,
                "jobs": usage.get(g["id"], (0, 0))[0],
                "cost_rub": round(float(usage.get(g["id"], (0, 0))[1]), 2),
            }
            for g in bot_access.guests()
        ]
    }


@router.post("/guests", dependencies=[Depends(require_auth)])
async def add_guest(body: GuestIn, db: AsyncSession = Depends(get_db)):
    try:
        return await bot_access.add(db, body.id, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.delete("/guests/{user_id}", dependencies=[Depends(require_auth)])
async def remove_guest(user_id: int, db: AsyncSession = Depends(get_db)):
    if not await bot_access.remove(db, user_id):
        raise HTTPException(404, "Нет такого пользователя")
    return {"ok": True}
