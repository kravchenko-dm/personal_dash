from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import check_password, issue_token, require_auth
from app.db import get_db
from app.schemas import LoginIn, TokenOut
from app.services import security

router = APIRouter(prefix="/api/auth", tags=["auth"])


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind Caddy, so this is the visitor's address
    return (request.client.host if request.client else "") or "unknown"


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)) -> TokenOut:
    try:
        await security.attempt(
            db, client_ip(request), request.headers.get("user-agent"), lambda: check_password(body.password)
        )
    except security.Locked as exc:
        minutes = max(1, round(exc.retry_after / 60))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Слишком много неверных паролей. Попробуйте через {minutes} мин.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except PermissionError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный пароль") from None
    return TokenOut(token=issue_token())


@router.get("/events", dependencies=[Depends(require_auth)])
async def login_events(db: AsyncSession = Depends(get_db)) -> dict:
    return await security.events(db)


@router.post("/logout-all", response_model=TokenOut, dependencies=[Depends(require_auth)])
async def logout_all(db: AsyncSession = Depends(get_db)) -> TokenOut:
    return TokenOut(token=await security.logout_everywhere(db))
