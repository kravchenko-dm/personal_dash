from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import AppleReminder
from app.schemas import TaskOut
from app.services import apple_reminders as svc
from app.services.apple_reminders import SyncIn

router = APIRouter(prefix="/api/apple", tags=["apple"], dependencies=[Depends(require_auth)])


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    notes: str | None
    list_name: str
    due_at: datetime | None
    priority: int
    flagged: bool
    url: str | None
    source: str
    imported_task_id: int | None
    synced_at: datetime


@router.post("/reminders/sync")
async def sync(body: SyncIn, db: AsyncSession = Depends(get_db)):
    """Called by the iOS Shortcut with a full snapshot of open reminders.
    Auth: Bearer header or ?token=... in the URL (easier to set up in Shortcuts)."""
    count = await svc.apply_snapshot(db, body.items, source="shortcuts")
    return {"ok": True, "stored": count}


@router.get("/reminders")
async def list_reminders(db: AsyncSession = Depends(get_db)):
    rows = await svc.list_reminders(db)
    return {"items": [ReminderOut.model_validate(r) for r in rows], "last_sync": await svc.last_sync(db)}


@router.post("/reminders/caldav-sync")
async def caldav_sync(db: AsyncSession = Depends(get_db)):
    try:
        return {"ok": True, "stored": await svc.sync_caldav(db)}
    except Exception as exc:
        raise HTTPException(502, f"iCloud CalDAV: {exc}") from exc


@router.post("/reminders/{reminder_id}/to-task", response_model=TaskOut)
async def to_task(reminder_id: int, db: AsyncSession = Depends(get_db)):
    reminder = await db.get(AppleReminder, reminder_id)
    if not reminder:
        raise HTTPException(404, "Not found")
    if reminder.imported_task_id:
        raise HTTPException(409, "Уже перенесено в задачи")
    return await svc.to_task(db, reminder)
