"""Dashboard login protection: a log of every attempt, per-IP lockout after repeated wrong
passwords, alerts on brute force and on logins from new addresses, "log out everywhere"."""

import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.models import LoginEvent, Setting
from app.services import push, telegram_bot

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=15)
MAX_FAILS = 5  # wrong passwords from one IP within WINDOW, then the IP is locked out until they age out
GLOBAL_WINDOW = timedelta(hours=1)
GLOBAL_ALERT = 20  # wrong passwords from all IPs within GLOBAL_WINDOW: a distributed attempt
KEEP = timedelta(days=90)
EPOCH_KEY = "auth.token_epoch"


class Locked(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


async def _alert(db: AsyncSession, text: str) -> None:
    try:
        await push.notify_all(db, "Безопасность", text, url="/settings", tag="security")
    except Exception as exc:  # noqa: BLE001
        log.warning("security push failed: %s", exc)
    try:
        await telegram_bot.send_to_owner(text)
    except Exception as exc:  # noqa: BLE001
        log.warning("security telegram failed: %s", exc)


def _short_ua(ua: str | None) -> str:
    ua = (ua or "").strip()
    return ua[:80] + ("…" if len(ua) > 80 else "")


async def _count(db: AsyncSession, since: datetime, ip: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(LoginEvent)
        .where(LoginEvent.result == "failed", LoginEvent.ts >= since)
    )
    if ip is not None:
        stmt = stmt.where(LoginEvent.ip == ip)
    return int(await db.scalar(stmt) or 0)


async def attempt(db: AsyncSession, ip: str, user_agent: str | None, password_ok_fn) -> None:
    """Record one login attempt. Raises Locked when the IP is locked out, PermissionError on a wrong
    password; returns normally on success. `password_ok_fn` is only called when the IP may try."""
    now = datetime.now(timezone.utc)
    ua = (user_agent or "")[:300] or None
    fails = await _count(db, now - WINDOW, ip)
    if fails >= MAX_FAILS:
        oldest = await db.scalar(
            select(func.min(LoginEvent.ts)).where(
                LoginEvent.ip == ip, LoginEvent.result == "failed", LoginEvent.ts >= now - WINDOW
            )
        )
        db.add(LoginEvent(ts=now, ip=ip, user_agent=ua, result="blocked"))
        await db.commit()
        oldest = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
        raise Locked(max(60, int((oldest + WINDOW - now).total_seconds())))

    if not password_ok_fn():
        db.add(LoginEvent(ts=now, ip=ip, user_agent=ua, result="failed"))
        await db.flush()
        if fails + 1 == MAX_FAILS:
            await _alert(
                db,
                f"🔐 Дашборд: {MAX_FAILS} неверных паролей подряд с {ip} за {int(WINDOW.total_seconds() // 60)} мин. "
                f"Вход с этого адреса закрыт на {int(WINDOW.total_seconds() // 60)} мин.\n{_short_ua(ua)}",
            )
        if await _count(db, now - GLOBAL_WINDOW) == GLOBAL_ALERT:
            await _alert(
                db,
                f"🔐 Дашборд: {GLOBAL_ALERT} неверных паролей за час с разных адресов — похоже на перебор.",
            )
        await db.commit()
        raise PermissionError

    seen_before = await db.scalar(
        select(func.count()).select_from(LoginEvent).where(LoginEvent.result == "ok", LoginEvent.ip == ip)
    )
    any_ok = await db.scalar(select(func.count()).select_from(LoginEvent).where(LoginEvent.result == "ok"))
    db.add(LoginEvent(ts=now, ip=ip, user_agent=ua, result="ok"))
    await db.execute(delete(LoginEvent).where(LoginEvent.ts < now - KEEP))
    await db.flush()
    if any_ok and not seen_before:
        await _alert(
            db,
            f"🔓 Вход в дашборд с нового адреса {ip}\n{_short_ua(ua)}\nЕсли это не вы — «Ещё → Выйти на всех устройствах» и смените пароль.",
        )
    await db.commit()


async def events(db: AsyncSession, limit: int = 30) -> dict:
    now = datetime.now(timezone.utc)
    rows = (await db.scalars(select(LoginEvent).order_by(LoginEvent.ts.desc()).limit(limit))).all()
    failed_24h = await _count(db, now - timedelta(hours=24))
    locked = (
        (
            await db.execute(
                select(LoginEvent.ip)
                .where(LoginEvent.result == "failed", LoginEvent.ts >= now - WINDOW)
                .group_by(LoginEvent.ip)
                .having(func.count() >= MAX_FAILS)
            )
        )
        .scalars()
        .all()
    )
    return {
        "events": [
            {"ts": r.ts.isoformat(), "ip": r.ip, "user_agent": r.user_agent, "result": r.result} for r in rows
        ],
        "failed_24h": failed_24h,
        "locked_ips": list(locked),
        "max_fails": MAX_FAILS,
        "window_minutes": int(WINDOW.total_seconds() // 60),
    }


async def load_epoch(db: AsyncSession) -> int:
    row = await db.get(Setting, EPOCH_KEY)
    epoch = int((row.value or {}).get("epoch", 0)) if row else 0
    auth.set_token_epoch(epoch)
    return epoch


async def logout_everywhere(db: AsyncSession) -> str:
    """Void every token issued so far and return a fresh one for the caller's device."""
    epoch = int(time.time())
    row = await db.get(Setting, EPOCH_KEY)
    if row:
        row.value = {"epoch": epoch}
    else:
        db.add(Setting(key=EPOCH_KEY, value={"epoch": epoch}))
    await db.commit()
    auth.set_token_epoch(epoch)
    return auth.issue_token()
