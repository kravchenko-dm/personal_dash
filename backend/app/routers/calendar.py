from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.services import calendar as svc

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class SelectedIn(BaseModel):
    ids: list[str]


@router.get("/oauth/start", dependencies=[Depends(require_auth)])
async def oauth_start():
    if not get_settings().google_enabled:
        raise HTTPException(400, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET не заданы")
    return {"url": svc.auth_url()}


@router.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(code: str, state: str, db: AsyncSession = Depends(get_db)):
    try:
        await svc.finish_oauth(db, code, state)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse("/calendar?connected=1")


@router.post("/disconnect", dependencies=[Depends(require_auth)])
async def disconnect(db: AsyncSession = Depends(get_db)):
    await svc.disconnect(db)
    return {"ok": True}


@router.get("/calendars", dependencies=[Depends(require_auth)])
async def calendars(db: AsyncSession = Depends(get_db)):
    if not await svc.is_connected(db):
        raise HTTPException(400, "Google Calendar не подключён")
    return {"calendars": await svc.list_calendars(db), "selected": await svc.selected_calendars(db)}


@router.put("/calendars/selected", dependencies=[Depends(require_auth)])
async def set_selected(body: SelectedIn, db: AsyncSession = Depends(get_db)):
    await svc.set_selected_calendars(db, body.ids)
    return {"ok": True}


@router.get("/events", dependencies=[Depends(require_auth)])
async def events(days: int = 7, db: AsyncSession = Depends(get_db)):
    if not await svc.is_connected(db):
        return {"connected": False, "events": []}
    try:
        return {"connected": True, "events": await svc.list_events(db, days=days)}
    except Exception as exc:
        raise HTTPException(502, f"Google Calendar: {exc}") from exc
