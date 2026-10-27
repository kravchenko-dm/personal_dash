from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.integrations.registry import PROVIDERS, describe_providers
from app.models import BalanceAccount, BalanceSample
from app.schemas import BalanceAccountIn, BalanceAccountOut, BalanceSampleOut, ProviderInfo
from app.services import balances as svc


def _masked(account: BalanceAccount) -> BalanceAccountOut:
    """Never return API keys to the browser; the UI sends '••••••' back to keep them."""
    out = BalanceAccountOut.model_validate(account)
    secret_keys = (
        {f.key for f in PROVIDERS[account.provider].fields if f.kind == "secret"}
        if account.provider in PROVIDERS
        else set()
    )
    out.config = {k: ("••••••" if k in secret_keys and v else v) for k, v in account.config.items()}
    return out


router = APIRouter(prefix="/api/balances", tags=["balances"], dependencies=[Depends(require_auth)])


@router.get("/providers", response_model=list[ProviderInfo])
async def providers():
    return describe_providers()


@router.get("", response_model=list[BalanceAccountOut])
async def list_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(BalanceAccount).order_by(BalanceAccount.id))).all()
    return [_masked(a) for a in rows]


@router.post("", response_model=BalanceAccountOut, status_code=201)
async def create_account(body: BalanceAccountIn, db: AsyncSession = Depends(get_db)):
    cls = PROVIDERS.get(body.provider)
    if not cls:
        raise HTTPException(400, "Неизвестный провайдер")
    try:
        cls.validate_config(body.config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    account = BalanceAccount(**body.model_dump())
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return _masked(await svc.refresh_account(db, account))


@router.put("/{account_id}", response_model=BalanceAccountOut)
async def update_account(account_id: int, body: BalanceAccountIn, db: AsyncSession = Depends(get_db)):
    account = await db.get(BalanceAccount, account_id)
    if not account:
        raise HTTPException(404, "Not found")
    cls = PROVIDERS.get(body.provider)
    if not cls:
        raise HTTPException(400, "Неизвестный провайдер")
    # keep secrets if the UI sent the masked placeholder back
    merged = dict(account.config)
    for key, value in body.config.items():
        if value != "••••••":
            merged[key] = value
    for key, value in body.model_dump(exclude={"config"}).items():
        setattr(account, key, value)
    account.config = merged
    await db.commit()
    return _masked(await svc.refresh_account(db, account))


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(BalanceAccount, account_id)
    if not account:
        raise HTTPException(404, "Not found")
    await db.delete(account)
    await db.commit()


@router.post("/{account_id}/refresh", response_model=BalanceAccountOut)
async def refresh_one(account_id: int, db: AsyncSession = Depends(get_db)):
    account = await db.get(BalanceAccount, account_id)
    if not account:
        raise HTTPException(404, "Not found")
    return _masked(await svc.refresh_account(db, account))


@router.post("/refresh", response_model=list[BalanceAccountOut])
async def refresh_all(db: AsyncSession = Depends(get_db)):
    return [_masked(a) for a in await svc.refresh_all(db)]


@router.get("/{account_id}/history", response_model=list[BalanceSampleOut])
async def history(account_id: int, limit: int = 200, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(BalanceSample)
        .where(BalanceSample.account_id == account_id)
        .order_by(BalanceSample.taken_at.desc())
        .limit(limit)
    )
    rows = list((await db.scalars(stmt)).all())
    return list(reversed(rows))
