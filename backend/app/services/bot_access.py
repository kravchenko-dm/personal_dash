"""Who may talk to the Telegram bot besides the owner (TELEGRAM_OWNER_ID).

Guests may only transcribe their own recordings in a private chat with the bot. Everything else
(tasks, chat summaries, groups) stays owner-only. A stranger asks for access with /start, the owner
approves with a button; the list is also editable in the UI (Settings -> Пользователи бота).
The list lives in the settings table and is cached in memory (one backend process)."""

import time
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Setting

KEY = "telegram.guests"
REQUEST_EVERY = 12 * 3600  # one access request per stranger per 12 hours

_guests: dict[int, dict] = {}
_requested: dict[int, float] = {}


def is_owner(user_id: int | None) -> bool:
    owner = get_settings().telegram_owner_id
    return owner is None or user_id == owner


def is_guest(user_id: int | None) -> bool:
    return user_id is not None and user_id in _guests


def can_use(user_id: int | None) -> bool:
    return is_owner(user_id) or is_guest(user_id)


def guests() -> list[dict]:
    return sorted(({"id": uid, **g} for uid, g in _guests.items()), key=lambda g: g.get("added_at", ""))


def guest_name(user_id: int) -> str:
    g = _guests.get(user_id) or {}
    return g.get("name") or (f"@{g['username']}" if g.get("username") else str(user_id))


async def load(db: AsyncSession) -> None:
    row = await db.get(Setting, KEY)
    _guests.clear()
    for g in (row.value if row else {}).get("guests", []):
        _guests[int(g["id"])] = {k: v for k, v in g.items() if k != "id"}


async def _save(db: AsyncSession) -> None:
    value = {"guests": guests()}
    row = await db.get(Setting, KEY)
    if row:
        row.value = value
    else:
        db.add(Setting(key=KEY, value=value))
    await db.commit()


async def add(db: AsyncSession, user_id: int, name: str = "", username: str = "") -> dict:
    if is_owner(user_id) and get_settings().telegram_owner_id is not None:
        raise ValueError("Это id владельца")
    _guests[user_id] = {
        "name": name.strip()[:100],
        "username": username.strip().lstrip("@")[:64],
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _requested.pop(user_id, None)
    await _save(db)
    return {"id": user_id, **_guests[user_id]}


async def remove(db: AsyncSession, user_id: int) -> bool:
    if _guests.pop(user_id, None) is None:
        return False
    await _save(db)
    return True


def should_ask_owner(user_id: int) -> bool:
    """Throttle access requests so a stranger cannot flood the owner."""
    now = time.monotonic()
    if now - _requested.get(user_id, -REQUEST_EVERY) < REQUEST_EVERY:
        return False
    _requested[user_id] = now
    return True
