"""Who may use the dashboard and the Telegram bot: the `users` table, cached in memory.

- The owner (users.id = 1, Telegram id from TELEGRAM_OWNER_ID) has everything.
- A member with level "workspace" has a personal workspace: own tasks, own chats collected by the bot,
  own summaries and digests, transcription with task buttons. Logs in to the web UI via the bot (/login).
- A member with level "media" may only transcribe their own recordings in a private chat with the bot.
- Everyone else: a stranger asks for access with /start, the owner picks the level with a button; the list
  is also editable in the UI (Ещё → Пользователи). Owners never see members' data, only counts.

One backend process, so the cache is authoritative; every change goes through this module."""

import time
from dataclasses import asdict, dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import OWNER_ID, User

LEVELS = ("media", "workspace")
REQUEST_EVERY = 12 * 3600  # one access request per stranger per 12 hours


@dataclass
class Account:
    id: int
    telegram_id: int | None
    name: str
    username: str
    role: str
    level: str
    token_epoch: int
    evening_digest: bool
    morning_brief: bool
    created_at: str

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def has_workspace(self) -> bool:
        return self.is_owner or self.level == "workspace"

    @property
    def display(self) -> str:
        return self.name or (f"@{self.username}" if self.username else str(self.telegram_id or self.id))

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "token_epoch"}


_by_id: dict[int, Account] = {}
_by_tg: dict[int, Account] = {}
_requested: dict[int, float] = {}


def _account(u: User) -> Account:
    return Account(
        id=u.id, telegram_id=u.telegram_id, name=u.name or "", username=u.username or "", role=u.role,
        level=u.level, token_epoch=int(u.token_epoch or 0), evening_digest=bool(u.evening_digest),
        morning_brief=bool(u.morning_brief),
        created_at=u.created_at.isoformat(timespec="seconds") if u.created_at else "",
    )  # fmt: skip


def _cache(u: User) -> Account:
    old = _by_id.get(u.id)
    if old and old.telegram_id in _by_tg:
        _by_tg.pop(old.telegram_id, None)
    acc = _account(u)
    _by_id[u.id] = acc
    if acc.telegram_id is not None:
        _by_tg[acc.telegram_id] = acc
    return acc


async def _owner_row(db: AsyncSession) -> User:
    owner = await db.get(User, OWNER_ID)
    if owner is None:  # a database created with create_all (tests) has no migration-made owner
        owner = User(id=OWNER_ID, role="owner", level="workspace")
        db.add(owner)
        await db.flush()
    return owner


async def load(db: AsyncSession) -> None:
    """Read every user; keep the owner's Telegram id in sync with TELEGRAM_OWNER_ID."""
    owner = await _owner_row(db)
    tid = get_settings().telegram_owner_id
    if tid is not None and owner.telegram_id != tid:
        clash = await db.scalar(select(User).where(User.telegram_id == tid, User.id != OWNER_ID))
        if clash:  # the owner was once a member: the owner row wins
            await db.delete(clash)
            await db.flush()
        owner.telegram_id = tid
    await db.commit()
    _by_id.clear()
    _by_tg.clear()
    for u in (await db.scalars(select(User))).all():
        _cache(u)


# ---------- lookups ----------


def owner() -> Account | None:
    return _by_id.get(OWNER_ID)


def get(user_id: int) -> Account | None:
    return _by_id.get(user_id)


def user_for(telegram_id: int | None) -> Account | None:
    """The account behind a Telegram user. Without TELEGRAM_OWNER_ID everyone counts as the owner."""
    if is_owner(telegram_id):
        return owner()
    return _by_tg.get(telegram_id) if telegram_id is not None else None


def is_owner(telegram_id: int | None) -> bool:
    tid = get_settings().telegram_owner_id
    return tid is None or telegram_id == tid


def is_guest(telegram_id: int | None) -> bool:
    """A member of any level (not the owner)."""
    acc = _by_tg.get(telegram_id) if telegram_id is not None else None
    return acc is not None and not acc.is_owner


def can_use(telegram_id: int | None) -> bool:
    return user_for(telegram_id) is not None


def has_workspace(telegram_id: int | None) -> bool:
    acc = user_for(telegram_id)
    return acc is not None and acc.has_workspace


def members() -> list[Account]:
    return sorted((a for a in _by_id.values() if not a.is_owner), key=lambda a: a.created_at)


def workspace_accounts() -> list[Account]:
    return [a for a in sorted(_by_id.values(), key=lambda a: a.id) if a.has_workspace]


def guest_name(telegram_id: int) -> str:
    acc = _by_tg.get(telegram_id)
    return acc.display if acc else str(telegram_id)


# ---------- changes ----------


async def add(
    db: AsyncSession, telegram_id: int, name: str = "", username: str = "", level: str = "media"
) -> Account:
    if get_settings().telegram_owner_id is not None and is_owner(telegram_id):
        raise ValueError("Это id владельца")
    if level not in LEVELS:
        raise ValueError("Неизвестный уровень доступа")
    row = await db.scalar(select(User).where(User.telegram_id == telegram_id))
    if row is None:
        row = User(telegram_id=telegram_id, role="member")
        db.add(row)
    row.name = (name or row.name or "").strip()[:100]
    row.username = (username or row.username or "").strip().lstrip("@")[:64]
    row.level = level
    await db.commit()
    await db.refresh(row)
    _requested.pop(telegram_id, None)
    return _cache(row)


async def update(db: AsyncSession, user_id: int, **fields) -> Account | None:
    row = await db.get(User, user_id)
    if row is None:
        return None
    if "level" in fields and (fields["level"] not in LEVELS or row.role == "owner"):
        raise ValueError("Уровень доступа владельца не меняется")
    for key in ("name", "level", "evening_digest", "morning_brief", "token_epoch"):
        if key in fields and fields[key] is not None:
            setattr(row, key, fields[key])
    await db.commit()
    await db.refresh(row)
    return _cache(row)


async def remove(db: AsyncSession, user_id: int) -> bool:
    """Deletes a member and everything that is theirs (tasks, chats, summaries, collector sources)."""
    row = await db.get(User, user_id)
    if row is None or row.role == "owner":
        return False
    from app.models import (
        InboxItem,
        MailAccount,
        MailRule,
        MaxAccount,
        MediaJob,
        PushSubscription,
        Summary,
        Task,
        TelegramChat,
        TelegramMessage,
    )
    from app.services import max_collector

    await max_collector.forget(user_id)  # stops the MAX client and wipes its session files

    # explicit deletes: SQLite does not enforce ON DELETE CASCADE without a pragma
    chat_ids = select(TelegramChat.chat_id).where(TelegramChat.user_id == user_id)
    await db.execute(delete(TelegramMessage).where(TelegramMessage.chat_id.in_(chat_ids)))
    owned = (TelegramChat, Summary, Task, PushSubscription, MailAccount, MailRule, MaxAccount, InboxItem)
    for model in owned:
        await db.execute(delete(model).where(model.user_id == user_id))
    if row.telegram_id is not None:
        await db.execute(delete(MediaJob).where(MediaJob.chat_id == row.telegram_id))
    await db.delete(row)
    await db.commit()
    acc = _by_id.pop(user_id, None)
    if acc and acc.telegram_id is not None:
        _by_tg.pop(acc.telegram_id, None)
    return True


async def log_out_everywhere(db: AsyncSession, user_id: int) -> None:
    await update(db, user_id, token_epoch=int(time.time()))


def should_ask_owner(telegram_id: int) -> bool:
    """Throttle access requests so a stranger cannot flood the owner."""
    now = time.monotonic()
    if now - _requested.get(telegram_id, -REQUEST_EVERY) < REQUEST_EVERY:
        return False
    _requested[telegram_id] = now
    return True
