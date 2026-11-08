from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.services import agenda, calendar_alerts, icloud
from app.services import calendar as svc

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class SelectedIn(BaseModel):
    ids: list[str]
    source: str = "google"


class AlertsIn(BaseModel):
    remind_minutes: int = Field(ge=0, le=1440)
    watch_changes: bool = True
    watch_days: int = Field(default=7, ge=1, le=30)


class EventIn(BaseModel):
    source: Literal["google", "icloud"]
    calendar_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    start: str  # ISO 8601 datetime with offset, or YYYY-MM-DD when all_day
    end: str | None = None
    all_day: bool = False
    location: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)

    def bounds(self) -> tuple[datetime | date, datetime | date]:
        try:
            if self.all_day:
                start = date.fromisoformat(self.start[:10])
                end = date.fromisoformat(self.end[:10]) if self.end else start + timedelta(days=1)
                if end <= start:
                    end = start + timedelta(days=1)
                return start, end
            start_dt = datetime.fromisoformat(self.start.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                raise ValueError("время без часового пояса")
            end_dt = (
                datetime.fromisoformat(self.end.replace("Z", "+00:00"))
                if self.end
                else start_dt + timedelta(hours=1)
            )
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(hours=1)
            return start_dt, end_dt
        except ValueError as exc:
            raise HTTPException(400, f"Неверная дата: {exc}") from exc


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
    """All calendars from every connected provider plus the selected ids per provider."""
    out: list[dict] = []
    errors: list[str] = []
    if await svc.is_connected(db):
        try:
            out += await svc.list_calendars(db)
        except Exception as exc:
            errors.append(f"google: {exc}")
    if icloud.enabled():
        try:
            out += await icloud.list_calendars()
        except Exception as exc:
            errors.append(f"icloud: {exc}")
    if not out and not errors:
        raise HTTPException(400, "Ни один календарь не подключён")
    return {
        "calendars": out,
        "selected": {
            "google": await svc.selected_calendars(db),
            "icloud": await icloud.selected_calendars(db),
        },
        "errors": errors,
    }


@router.put("/calendars/selected", dependencies=[Depends(require_auth)])
async def set_selected(body: SelectedIn, db: AsyncSession = Depends(get_db)):
    if body.source == "icloud":
        await icloud.set_selected_calendars(db, body.ids)
    else:
        await svc.set_selected_calendars(db, body.ids)
    return {"ok": True}


@router.get("/events", dependencies=[Depends(require_auth)])
async def events(days: int = 7, db: AsyncSession = Depends(get_db)):
    if not await agenda.connected(db):
        return {"connected": False, "events": [], "errors": []}
    found, errors = await agenda.events(db, days=days)
    return {"connected": True, "events": found, "errors": errors}


@router.post("/events", dependencies=[Depends(require_auth)], status_code=201)
async def create_event(body: EventIn, db: AsyncSession = Depends(get_db)):
    start, end = body.bounds()
    try:
        if body.source == "icloud":
            return await icloud.create_event(
                body.calendar_id, body.title, start, end, body.location, body.description
            )
        return await svc.create_event(
            db, body.calendar_id, body.title, start, end, body.location, body.description
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/alerts", dependencies=[Depends(require_auth)])
async def get_alerts(db: AsyncSession = Depends(get_db)):
    return {**(await calendar_alerts.get_config(db)), "choices": list(calendar_alerts.REMIND_CHOICES)}


@router.put("/alerts", dependencies=[Depends(require_auth)])
async def set_alerts(body: AlertsIn, db: AsyncSession = Depends(get_db)):
    try:
        cfg = await calendar_alerts.set_config(db, body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**cfg, "choices": list(calendar_alerts.REMIND_CHOICES)}
