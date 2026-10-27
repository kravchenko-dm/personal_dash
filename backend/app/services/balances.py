import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.registry import get_provider
from app.models import BalanceAccount, BalanceSample
from app.services import push

log = logging.getLogger(__name__)


async def refresh_account(db: AsyncSession, account: BalanceAccount) -> BalanceAccount:
    now = datetime.now(timezone.utc)
    try:
        provider = get_provider(account.provider)
        result = await provider.fetch(account.config)
        previous = account.last_value
        account.last_value = result.value
        account.last_extra = result.extra or None
        account.last_error = None
        if result.currency and not account.currency:
            account.currency = result.currency
        db.add(BalanceSample(account_id=account.id, value=result.value, taken_at=now))
        await _maybe_alert(db, account, previous)
    except Exception as exc:
        log.warning("balance %s (%s) failed: %s", account.name, account.provider, exc)
        account.last_error = str(exc)[:500]
    account.last_checked_at = now
    await db.commit()
    await db.refresh(account)
    return account


async def refresh_all(db: AsyncSession) -> list[BalanceAccount]:
    accounts = list((await db.scalars(select(BalanceAccount).where(BalanceAccount.enabled.is_(True)))).all())
    for account in accounts:
        if account.provider == "manual":
            continue
        await refresh_account(db, account)
    return accounts


async def _maybe_alert(db: AsyncSession, account: BalanceAccount, previous: float | None) -> None:
    threshold = account.alert_below
    if threshold is None or account.last_value is None:
        return
    below = account.last_value < threshold
    if below and (previous is None or previous >= threshold or account.alerted_at is None):
        account.alerted_at = datetime.now(timezone.utc)
        await push.notify_all(
            db,
            title=f"Низкий баланс: {account.name}",
            body=f"{account.last_value:g} {account.currency} (порог {threshold:g})",
            url="/balances",
        )
    elif not below:
        account.alerted_at = None
