"""Request collector, MAX: a user-account client (PyMax WebClient, the unofficial web API) per user that
forwards private messages to the user's bot chat. Read-only: it never sends anything to MAX.

Login is by QR like web.max.ru: the dashboard shows the QR the client asked for, the user scans it in
the MAX app. The session lives in DATA_DIR/<user_id>/session.db on the backend's data volume; the
`max_accounts` row only keeps the status. One backend process, so the clients live in `_clients`."""

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.db import SessionLocal
from app.models import MaxAccount
from app.services import bot_access, inbox

log = logging.getLogger(__name__)

DATA_DIR = Path("/app/data/max")
SESSION = "session.db"
RESTART_EVERY = 300  # the watchdog restarts a crashed client at most this often (seconds)
PASSWORD_WAIT = 600  # how long a 2FA password prompt waits for the dashboard

ATTACH_LABELS = {
    "PHOTO": "фото",
    "VIDEO": "видео",
    "AUDIO": "голосовое",
    "STICKER": "стикер",
    "CONTACT": "контакт",
    "SHARE": "ссылка",
    "POLL": "опрос",
    "CALL": "звонок",
}


@dataclass
class Runner:
    user_id: int
    client: Any = None
    task: asyncio.Task | None = None
    qr_url: str = ""
    password_hint: str | None = None
    password: asyncio.Future | None = None
    me_id: int | None = None
    names: dict[int, str] = field(default_factory=dict)
    dialogs: dict[int, bool] = field(default_factory=dict)  # chat id -> is a private dialog
    started_at: float = 0.0


_clients: dict[int, Runner] = {}


def session_dir(user_id: int) -> Path:
    return DATA_DIR / str(user_id)


def has_session(user_id: int) -> bool:
    return (session_dir(user_id) / SESSION).exists()


def runner(user_id: int) -> Runner | None:
    return _clients.get(user_id)


# ---------- account row ----------


async def _set(user_id: int, **fields) -> None:
    async with SessionLocal() as db:
        row = await db.get(MaxAccount, user_id)
        if row is None:
            row = MaxAccount(user_id=user_id)
            db.add(row)
        for key, value in fields.items():
            setattr(row, key, value)
        await db.commit()


# ---------- auth providers (what PyMax would otherwise ask on the console) ----------


class DashboardQr:
    def __init__(self, r: Runner) -> None:
        self.r = r

    async def show_qr(self, qr_url: str) -> None:
        self.r.qr_url = qr_url
        await _set(self.r.user_id, status="waiting_qr", last_error="")


class DashboardPassword:
    def __init__(self, r: Runner) -> None:
        self.r = r

    async def get_password(self, hint: str | None = None) -> str:
        self.r.qr_url = ""
        self.r.password_hint = hint or ""
        self.r.password = asyncio.get_running_loop().create_future()
        await _set(self.r.user_id, status="waiting_password", last_error="")
        try:
            return await asyncio.wait_for(self.r.password, PASSWORD_WAIT)
        finally:
            self.r.password = None
            self.r.password_hint = None


def submit_password(user_id: int, password: str) -> bool:
    r = _clients.get(user_id)
    if not r or not r.password or r.password.done():
        return False
    r.password.set_result(password)
    return True


# ---------- messages ----------


def user_name(user: Any) -> str:
    for n in getattr(user, "names", None) or []:
        full = n.name or " ".join(x for x in (n.first_name, n.last_name) if x)
        if full:
            return full.strip()
    return ""


def message_text(message: Any) -> str:
    """The text plus a short label for every attachment (media are not forwarded)."""
    parts = [message.text.strip()] if getattr(message, "text", "") else []
    for a in getattr(message, "attaches", None) or []:
        kind = getattr(getattr(a, "type", None), "value", None) or str(getattr(a, "type", "") or "")
        if kind in ("CONTROL", "INLINE_KEYBOARD"):
            continue
        if kind == "FILE":
            parts.append(f"[файл: {getattr(a, 'name', '') or 'без имени'}]")
        else:
            parts.append(f"[{ATTACH_LABELS.get(kind, 'вложение')}]")
    return "\n".join(p for p in parts if p)


async def _is_dialog(r: Runner, client: Any, chat_id: int) -> bool:
    if chat_id in r.dialogs:
        return r.dialogs[chat_id]
    chat = next((c for c in (client.chats or []) if c.id == chat_id), None)
    if chat is None:
        try:
            chat = await client.get_chat(chat_id)
        except Exception as exc:
            log.warning("max user=%s: chat %s lookup failed: %s", r.user_id, chat_id, exc)
            return False
    kind = getattr(chat.type, "value", chat.type)
    r.dialogs[chat_id] = kind == "DIALOG"
    return r.dialogs[chat_id]


async def _name(r: Runner, client: Any, sender: int) -> str:
    if sender in r.names:
        return r.names[sender]
    name = ""
    try:
        user = await client.get_user(sender)
        name = user_name(user) if user else ""
    except Exception as exc:
        log.warning("max user=%s: user %s lookup failed: %s", r.user_id, sender, exc)
    r.names[sender] = name or f"id {sender}"
    return r.names[sender]


async def handle_message(r: Runner, message: Any, client: Any) -> None:
    if message.chat_id is None or message.sender is None or message.sender == r.me_id:
        return
    if not await _is_dialog(r, client, message.chat_id):
        return
    text = message_text(message)
    if not text:
        return
    acc = bot_access.get(r.user_id)
    if acc is None or not acc.has_workspace:
        return
    name = await _name(r, client, message.sender)
    async with SessionLocal() as db:
        item = await inbox.store(db, r.user_id, "max", f"{message.chat_id}:{message.id}", name, "", text)
    if item is None:
        return
    await inbox.deliver(r.user_id, f"💬 MAX, «{name}»:\n{text}", item.id)


# ---------- lifecycle ----------


def _build(r: Runner) -> Any:
    from pymax import ExtraConfig, QrAuthFlow, WebClient

    path = session_dir(r.user_id)
    path.mkdir(parents=True, exist_ok=True)
    client = WebClient(
        session_name=SESSION,
        work_dir=str(path),
        # relogin=False: a revoked session must not silently wait for a QR nobody is looking at
        extra_config=ExtraConfig(reconnect=True, reconnect_delay=10, relogin=False, log_level="WARNING"),
        auth_flow=QrAuthFlow(DashboardQr(r), DashboardPassword(r)),
    )

    @client.on_start()
    async def _started(c: Any) -> None:
        me = c.me.contact if c.me else None
        r.me_id = me.id if me else None
        r.qr_url = ""
        await _set(
            r.user_id, status="online", enabled=True, last_error="",
            me_name=user_name(me) if me else "", connected_at=datetime.now(timezone.utc),
        )  # fmt: skip
        log.info("max user=%s: online", r.user_id)

    @client.on_message()
    async def _message(message: Any, c: Any) -> None:
        try:
            await handle_message(r, message, c)
        except Exception:
            log.exception("max user=%s: message handling failed", r.user_id)

    return client


def _revoked(exc: Exception) -> bool:
    return any(s in str(exc) for s in ("FAIL_LOGIN_TOKEN", "FAIL_LOGOUT_ALL"))


async def _run(r: Runner) -> None:
    had_session = has_session(r.user_id)
    try:
        await r.client.start()
        await _set(r.user_id, status="off")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if _revoked(exc):
            log.info("max user=%s: session revoked", r.user_id)
            await _drop_session(r.user_id)
            await _set(r.user_id, status="logged_out", enabled=False, last_error="")
            await inbox.deliver(
                r.user_id, "💬 MAX отключён: сессию закрыли. Подключите заново по QR в «Сборщике»."
            )
        elif "QR authentication expired" in str(exc):
            await _set(r.user_id, status="off", last_error="QR-код истёк, покажите новый")
            if not had_session:
                await _drop_session(r.user_id)
        else:
            log.warning("max user=%s: client stopped: %s", r.user_id, exc)
            await _set(r.user_id, status="error", last_error=str(exc)[:500] or exc.__class__.__name__)
    finally:
        r.qr_url = ""
        if _clients.get(r.user_id) is r:
            _clients.pop(r.user_id, None)


async def start(user_id: int) -> None:
    """Starts (or keeps) the user's client; without a session it shows a QR in the dashboard."""
    r = _clients.get(user_id)
    if r and r.task and not r.task.done():
        return
    r = Runner(user_id=user_id, started_at=time.monotonic())
    _clients[user_id] = r
    await _set(user_id, status="connecting", last_error="")
    r.client = _build(r)
    r.task = asyncio.create_task(_run(r), name=f"max-{user_id}")


async def stop(user_id: int) -> None:
    r = _clients.pop(user_id, None)
    if not r:
        return
    if r.client:
        try:
            await r.client.close()
        except Exception:
            pass
    if r.task and not r.task.done():
        r.task.cancel()
        try:
            await r.task
        except (asyncio.CancelledError, Exception):
            pass


async def _drop_session(user_id: int) -> None:
    await asyncio.to_thread(shutil.rmtree, session_dir(user_id), True)


async def forget(user_id: int) -> None:
    """Stops the client and deletes the session files (the row is the caller's business)."""
    await stop(user_id)
    await _drop_session(user_id)


async def logout(user_id: int) -> None:
    await forget(user_id)
    await _set(user_id, status="off", enabled=False, me_name="", last_error="", connected_at=None)


async def startup() -> None:
    """At boot: bring back every enabled account that has a session."""
    from sqlalchemy import select

    async with SessionLocal() as db:
        rows = (await db.scalars(select(MaxAccount).where(MaxAccount.enabled.is_(True)))).all()
        ids = [row.user_id for row in rows]
    for user_id in ids:
        acc = bot_access.get(user_id)
        if acc and acc.has_workspace and has_session(user_id):
            await start(user_id)


async def watchdog() -> None:
    """Every minute: restart clients that died (network errors past PyMax's own reconnect)."""
    from sqlalchemy import select

    async with SessionLocal() as db:
        rows = (
            await db.scalars(
                select(MaxAccount).where(
                    MaxAccount.enabled.is_(True), MaxAccount.status.in_(("error", "off"))
                )
            )
        ).all()
        ids = [row.user_id for row in rows]
    for user_id in ids:
        if user_id in _clients or not has_session(user_id):
            continue
        last = _last_restart.get(user_id, 0.0)
        if time.monotonic() - last < RESTART_EVERY:
            continue
        _last_restart[user_id] = time.monotonic()
        log.info("max user=%s: restarting client", user_id)
        await start(user_id)


_last_restart: dict[int, float] = {}


async def shutdown() -> None:
    for user_id in list(_clients):
        await stop(user_id)


def qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")
