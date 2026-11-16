from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth, require_owner
from app.config import get_settings
from app.db import get_db
from app.models import MediaJob, Summary, Task, TelegramChat
from app.schemas import TelegramChatOut
from app.services import bot_access, telegram_bot
from app.services.bot_access import Account

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


@router.get("/chats", response_model=list[TelegramChatOut])
async def chats(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    stmt = select(TelegramChat).where(TelegramChat.user_id == user.id).order_by(TelegramChat.title)
    return list((await db.scalars(stmt)).all())


@router.patch("/chats/{chat_id}", response_model=TelegramChatOut)
async def toggle_chat(
    chat_id: int, collect: bool, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    chat = await db.get(TelegramChat, chat_id)
    if not chat or chat.user_id != user.id:
        raise HTTPException(404, "Not found")
    chat.collect = collect
    await db.commit()
    await db.refresh(chat)
    return chat


# ---------- users (owner only): who they are and how much they use, never their data ----------


class UserIn(BaseModel):
    telegram_id: int = Field(gt=0)
    name: str = ""
    level: str = "workspace"


class UserPatch(BaseModel):
    name: str | None = None
    level: str | None = None


async def _usage(db: AsyncSession) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in await db.execute(
        select(MediaJob.chat_id, func.count(), func.coalesce(func.sum(MediaJob.cost_rub), 0)).group_by(
            MediaJob.chat_id
        )
    ):
        out.setdefault(row[0], {})["media"] = (row[1], round(float(row[2]), 2))
    for model, key in ((TelegramChat, "chats"), (Summary, "summaries"), (Task, "tasks")):
        for uid, n in await db.execute(select(model.user_id, func.count()).group_by(model.user_id)):
            out.setdefault(-uid, {})[key] = n  # negative keys: users.id (positive: Telegram ids)
    return out


@router.get("/users", dependencies=[Depends(require_owner)])
async def list_users(db: AsyncSession = Depends(get_db)):
    usage = await _usage(db)
    users = []
    for a in bot_access.members():
        media = usage.get(a.telegram_id or 0, {}).get("media", (0, 0))
        mine = usage.get(-a.id, {})
        users.append(
            {
                **a.public(), "jobs": media[0], "cost_rub": media[1], "chats": mine.get("chats", 0),
                "summaries": mine.get("summaries", 0), "tasks": mine.get("tasks", 0),
            }
        )  # fmt: skip
    return {"users": users}


@router.post("/users", dependencies=[Depends(require_owner)])
async def add_user(body: UserIn, db: AsyncSession = Depends(get_db)):
    try:
        return (await bot_access.add(db, body.telegram_id, body.name, level=body.level)).public()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.patch("/users/{user_id}", dependencies=[Depends(require_owner)])
async def update_user(user_id: int, body: UserPatch, db: AsyncSession = Depends(get_db)):
    try:
        acc = await bot_access.update(db, user_id, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if acc is None or acc.is_owner:
        raise HTTPException(404, "Нет такого пользователя")
    return acc.public()


@router.delete("/users/{user_id}", dependencies=[Depends(require_owner)])
async def remove_user(user_id: int, db: AsyncSession = Depends(get_db)):
    """Removes the member and everything that is theirs."""
    if not await bot_access.remove(db, user_id):
        raise HTTPException(404, "Нет такого пользователя")
    return {"ok": True}
