from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth as auth_core
from app.auth import check_password, issue_token, require_auth, require_owner
from app.db import get_db
from app.schemas import LoginIn, TokenOut
from app.services import security
from app.services.bot_access import Account

router = APIRouter(prefix="/api/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind Caddy, so this is the visitor's address
    return (request.client.host if request.client else "") or "unknown"


class CodeIn(BaseModel):
    code: str


async def _login(db: AsyncSession, request: Request, check, method: str, wrong: str) -> TokenOut:
    try:
        user_id = await security.attempt(
            db, client_ip(request), request.headers.get("user-agent"), check, method=method
        )
    except security.Locked as exc:
        minutes = max(1, round(exc.retry_after / 60))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Слишком много неудачных попыток. Попробуйте через {minutes} мин.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except PermissionError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, wrong) from None
    return TokenOut(token=issue_token(user_id))


@router.get("/options")
async def login_options() -> dict:
    """Public: what the login page can offer (the bot for "Войти через Telegram")."""
    from app.services import telegram_bot

    return {"bot_username": telegram_bot.bot_username()}


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    return await _login(db, request, lambda: check_password(body.password), "password", "Неверный пароль")


@router.post("/telegram", response_model=TokenOut)
async def login_telegram(body: CodeIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    """One-time code from the bot's /login link."""

    def check():
        user_id = auth_core.redeem_login_code(body.code)
        return user_id if user_id is not None and auth_core.bot_access.get(user_id) else None

    return await _login(
        db, request, check, "telegram", "Ссылка для входа устарела или уже использована. Напишите боту /login"
    )


@router.get("/me")
async def me(user: Account = Depends(require_auth)) -> dict:
    return {
        "id": user.id, "name": user.display if not user.is_owner else (user.name or "Владелец"),
        "role": user.role, "level": user.level, "evening_digest": user.evening_digest,
        "morning_brief": user.morning_brief,
    }  # fmt: skip


class PrefsIn(BaseModel):
    evening_digest: bool | None = None
    morning_brief: bool | None = None


@router.patch("/me")
async def update_me(body: PrefsIn, user: Account = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    await auth_core.bot_access.update(db, user.id, **body.model_dump())
    return await me(auth_core.bot_access.get(user.id))


@router.get("/events", dependencies=[Depends(require_owner)])
async def login_events(db: AsyncSession = Depends(get_db)) -> dict:
    return await security.events(db)


@router.post("/logout-all", response_model=TokenOut)
async def logout_all(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)) -> TokenOut:
    return TokenOut(token=await security.logout_everywhere(db, user.id))
