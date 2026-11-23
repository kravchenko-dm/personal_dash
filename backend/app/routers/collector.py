"""Request collector settings of the signed-in user: their mailbox + suppliers, their MAX account."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import MailAccount, MailRule, MaxAccount
from app.services import mail_collector, max_collector
from app.services.bot_access import Account

router = APIRouter(prefix="/api/collector", tags=["collector"])


# ---------- mail ----------


class MailIn(BaseModel):
    host: str = Field(default="imap.mail.ru", min_length=3, max_length=200)
    port: int = Field(default=993, ge=1, le=65535)
    login: str = Field(min_length=3, max_length=200)
    password: str | None = Field(default=None, max_length=300)  # empty: keep the saved one
    folder: str = Field(default="INBOX", min_length=1, max_length=200)
    enabled: bool = True


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    patterns: list[str] | str
    enabled: bool = True


class RulePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    patterns: list[str] | str | None = None
    enabled: bool | None = None


def _rule(r: MailRule) -> dict:
    return {"id": r.id, "name": r.name, "patterns": r.patterns or [], "enabled": r.enabled}


def _account(a: MailAccount | None) -> dict | None:
    if a is None:
        return None
    return {
        "host": a.host, "port": a.port, "login": a.login, "folder": a.folder, "enabled": a.enabled,
        "has_password": bool(a.password), "ready": a.last_uid is not None,
        "last_check_at": a.last_check_at.isoformat() if a.last_check_at else None, "last_error": a.last_error,
    }  # fmt: skip


async def _mailbox(db: AsyncSession, user_id: int) -> MailAccount | None:
    return await db.scalar(select(MailAccount).where(MailAccount.user_id == user_id))


async def _mail_state(db: AsyncSession, user_id: int) -> dict:
    rules = (
        await db.scalars(select(MailRule).where(MailRule.user_id == user_id).order_by(MailRule.id))
    ).all()
    return {"account": _account(await _mailbox(db, user_id)), "rules": [_rule(r) for r in rules]}


@router.get("/mail")
async def get_mail(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    return await _mail_state(db, user.id)


@router.put("/mail")
async def put_mail(body: MailIn, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    row = await _mailbox(db, user.id)
    password = body.password or (row.password if row else "")
    if not password:
        raise HTTPException(400, "Нужен пароль приложения")
    host, login = body.host.strip(), body.login.strip()
    if body.enabled:
        try:
            await asyncio.to_thread(mail_collector.check_login, host, body.port, login, password, body.folder)
        except mail_collector.MailError as exc:
            raise HTTPException(400, str(exc)) from exc
    moved = row is None or (row.host, row.login, row.folder) != (host, login, body.folder)
    if row is None:
        row = MailAccount(user_id=user.id, login=login, password=password)
        db.add(row)
    row.host, row.port, row.login, row.password = host, body.port, login, password
    row.folder, row.enabled = body.folder, body.enabled
    row.last_error, row.errors = "", 0
    if moved:  # another mailbox: start from its current position, not from old UIDs
        row.uidvalidity = row.last_uid = None
    await db.commit()
    if body.enabled and row.last_uid is None:
        await mail_collector.collect_account(db, row)  # remembers the position right away
    return await _mail_state(db, user.id)


@router.delete("/mail", status_code=204)
async def delete_mail(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    row = await _mailbox(db, user.id)
    if row:
        await db.delete(row)
        await db.commit()


@router.post("/mail/rules", status_code=201)
async def add_rule(body: RuleIn, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    patterns = mail_collector.normalize_patterns(body.patterns)
    if not patterns:
        raise HTTPException(400, "Укажите адрес или домен поставщика")
    rule = MailRule(user_id=user.id, name=body.name.strip(), patterns=patterns, enabled=body.enabled)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return _rule(rule)


async def _own_rule(db: AsyncSession, rule_id: int, user_id: int) -> MailRule:
    rule = await db.get(MailRule, rule_id)
    if not rule or rule.user_id != user_id:
        raise HTTPException(404, "Поставщик не найден")
    return rule


@router.patch("/mail/rules/{rule_id}")
async def patch_rule(
    rule_id: int, body: RulePatch, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    rule = await _own_rule(db, rule_id, user.id)
    if body.name is not None:
        rule.name = body.name.strip()
    if body.patterns is not None:
        patterns = mail_collector.normalize_patterns(body.patterns)
        if not patterns:
            raise HTTPException(400, "Укажите адрес или домен поставщика")
        rule.patterns = patterns
    if body.enabled is not None:
        rule.enabled = body.enabled
    await db.commit()
    return _rule(rule)


@router.delete("/mail/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    await db.delete(await _own_rule(db, rule_id, user.id))
    await db.commit()


# ---------- MAX ----------


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


async def _max_state(db: AsyncSession, user_id: int) -> dict:
    row = await db.get(MaxAccount, user_id)
    r = max_collector.runner(user_id)
    status = row.status if row else "off"
    out = {
        "status": status,
        "enabled": bool(row and row.enabled),
        "me_name": row.me_name if row else "",
        "last_error": row.last_error if row else "",
        "connected_at": row.connected_at.isoformat() if row and row.connected_at else None,
        "running": bool(r and r.task and not r.task.done()),
        "qr_svg": None,
        "password_hint": None,
    }
    if r and status == "waiting_qr" and r.qr_url:
        out["qr_svg"] = max_collector.qr_svg(r.qr_url)
    if r and status == "waiting_password":
        out["password_hint"] = r.password_hint or ""
    return out


@router.get("/max")
async def get_max(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    return await _max_state(db, user.id)


@router.post("/max/connect")
async def connect_max(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    await max_collector.start(user.id)
    for _ in range(30):  # up to ~6 s for the QR (or the restored session) to show up
        await asyncio.sleep(0.2)
        db.expire_all()
        state = await _max_state(db, user.id)
        if state["status"] not in ("connecting",):
            return state
    return await _max_state(db, user.id)


@router.post("/max/password")
async def max_password(
    body: PasswordIn, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    if not max_collector.submit_password(user.id, body.password):
        raise HTTPException(409, "MAX сейчас не ждёт пароль")
    await asyncio.sleep(2)
    db.expire_all()
    return await _max_state(db, user.id)


@router.post("/max/logout")
async def logout_max(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    await max_collector.logout(user.id)
    db.expire_all()
    return await _max_state(db, user.id)
