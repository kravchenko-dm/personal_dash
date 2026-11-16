import asyncio
import json
import logging

from pywebpush import WebPushException, webpush
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import OWNER_ID, PushSubscription

log = logging.getLogger(__name__)


def _send_sync(sub: PushSubscription, payload: dict) -> bool:
    """Returns False when the subscription is gone (410/404) and should be deleted."""
    s = get_settings()
    try:
        webpush(
            subscription_info={"endpoint": sub.endpoint, "keys": sub.keys},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=s.vapid_private_key,
            vapid_claims={"sub": s.vapid_subject},
            ttl=3600,
        )
        return True
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in (404, 410):
            return False
        log.warning("push failed (%s): %s", status, exc)
        return True


async def _notify(db: AsyncSession, user_id: int, title: str, body: str, url: str, tag: str | None) -> int:
    if not get_settings().push_enabled:
        return 0
    subs = list((await db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id))).all())
    if not subs:
        return 0
    payload = {"title": title, "body": body, "url": url, "tag": tag}
    results = await asyncio.gather(*(asyncio.to_thread(_send_sync, sub, payload) for sub in subs))
    dead = [sub.id for sub, ok in zip(subs, results, strict=True) if not ok]
    if dead:
        await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(dead)))
        await db.commit()
    return sum(1 for ok in results if ok)


async def notify_user(
    db: AsyncSession, user_id: int, title: str, body: str, url: str = "/", tag: str | None = None
) -> int:
    """Push to one user's devices."""
    return await _notify(db, user_id, title, body, url, tag)


async def notify_owner(
    db: AsyncSession, title: str, body: str, url: str = "/", tag: str | None = None
) -> int:
    """System alerts (monitoring, backups, balances, security, calendar, YouGile): the owner's devices only."""
    return await _notify(db, OWNER_ID, title, body, url, tag)
