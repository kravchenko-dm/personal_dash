"""Apple Reminders.

Reliable path: an iOS Shortcut posts a snapshot of open reminders to /api/apple/reminders/sync
(see README). Fallback: CalDAV VTODO for lists that were never upgraded to the post-iOS-13 format."""

import asyncio
import hashlib
import logging
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppleReminder, Setting, Task
from app.services import icloud

log = logging.getLogger(__name__)
LAST_SYNC_KEY = "apple.reminders_last_sync"
PRIORITY_WORDS = {"high": 1, "высокий": 1, "medium": 5, "средний": 5, "low": 9, "низкий": 9}


class ReminderIn(BaseModel):
    """Lenient: Shortcuts sends whatever 'Get Details of Reminder' produces."""

    title: str = Field(min_length=1)
    notes: str | None = None
    list: str = ""
    due: datetime | None = None
    priority: int = 0
    completed: bool = False
    flagged: bool = False
    url: str | None = None

    @field_validator("due", mode="before")
    @classmethod
    def _parse_due(cls, v: Any) -> datetime | None:
        if v in (None, "", "null"):
            return None
        if isinstance(v, datetime):
            return v
        text = str(v).strip()
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y, %H:%M",
            "%d.%m.%Y",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(text.replace("Z", "+0000"), fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            log.info("apple reminders: unparsed due date %r", text)
            return None

    @field_validator("priority", mode="before")
    @classmethod
    def _parse_priority(cls, v: Any) -> int:
        if v in (None, ""):
            return 0
        if isinstance(v, bool):
            return 0
        if isinstance(v, int | float):
            return int(v)
        text = str(v).strip().lower()
        if text.isdigit():
            return int(text)
        return PRIORITY_WORDS.get(text, 0)

    @field_validator("completed", "flagged", mode="before")
    @classmethod
    def _parse_bool(cls, v: Any) -> bool:
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "да")

    @field_validator("notes", "url", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> str | None:
        return None if v in (None, "") else str(v)


class SyncIn(BaseModel):
    items: list[ReminderIn] = Field(default_factory=list)

    @field_validator("items", mode="before")
    @classmethod
    def _single_to_list(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return [v]
        return v or []


def _key(list_name: str, title: str, due: datetime | None) -> str:
    raw = f"{list_name}\x00{title}\x00{due.isoformat() if due else ''}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def apply_snapshot(db: AsyncSession, items: list[ReminderIn], source: str = "shortcuts") -> int:
    """Replace all reminders of `source` with the snapshot, keeping task links for unchanged ones."""
    existing = {
        r.key: r
        for r in (await db.scalars(select(AppleReminder).where(AppleReminder.source == source))).all()
    }
    now = datetime.now(timezone.utc)
    kept = 0
    seen: set[str] = set()
    for item in items:
        if item.completed:
            continue
        due = _aware(item.due)
        key = _key(item.list, item.title, due)
        if key in seen:
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = AppleReminder(key=key, source=source)
            db.add(row)
        row.title = item.title
        row.notes = item.notes
        row.list_name = item.list
        row.due_at = due
        row.priority = item.priority
        row.flagged = item.flagged
        row.url = item.url
        row.synced_at = now
        kept += 1
    stale = [r.id for k, r in existing.items() if k not in seen]
    if stale:
        await db.execute(delete(AppleReminder).where(AppleReminder.id.in_(stale)))
    setting = await db.get(Setting, LAST_SYNC_KEY)
    payload = {"at": now.isoformat(), "count": kept, "source": source}
    if setting:
        setting.value = payload
    else:
        db.add(Setting(key=LAST_SYNC_KEY, value=payload))
    await db.commit()
    return kept


async def last_sync(db: AsyncSession) -> dict | None:
    row = await db.get(Setting, LAST_SYNC_KEY)
    return row.value if row else None


async def list_reminders(db: AsyncSession) -> list[AppleReminder]:
    stmt = select(AppleReminder).order_by(
        AppleReminder.due_at.nulls_last(), AppleReminder.list_name, AppleReminder.title
    )
    return list((await db.scalars(stmt)).all())


async def to_task(db: AsyncSession, reminder: AppleReminder) -> Task:
    prio = 1 if reminder.priority in (1, 2, 3) else 3 if reminder.priority >= 7 else 2
    task = Task(
        title=reminder.title,
        notes="\n".join(
            filter(
                None,
                [
                    reminder.notes,
                    f"Список Apple: {reminder.list_name}" if reminder.list_name else "",
                    reminder.url or "",
                ],
            )
        )
        or None,
        priority=prio,
        due_at=reminder.due_at,
        source="apple",
        source_ref=reminder.key,
    )
    db.add(task)
    await db.flush()
    reminder.imported_task_id = task.id
    await db.commit()
    await db.refresh(task)
    return task


def reminders_text(rows: list[AppleReminder], limit: int = 15) -> str:
    if not rows:
        return ""
    lines = []
    for r in rows[:limit]:
        due = f" · до {r.due_at.astimezone():%d.%m %H:%M}" if r.due_at else ""
        lines.append(f"• [{r.list_name}] {r.title}{due}" if r.list_name else f"• {r.title}{due}")
    return "\n".join(lines)


# ---------- CalDAV fallback (legacy, non-upgraded reminder lists only) ----------


def _caldav_todos_sync() -> list[ReminderIn]:
    out: list[ReminderIn] = []
    with icloud._client() as client:
        for cal in client.principal().calendars():
            try:
                comps = cal.get_supported_components()
            except Exception:
                comps = []
            if comps and "VTODO" not in comps:
                continue
            try:
                todos = cal.search(todo=True, include_completed=False)
            except Exception as exc:
                log.info("caldav todo search skipped for %s: %s", cal.url, exc)
                continue
            for item in todos:
                comp = item.icalendar_component
                if comp is None or comp.name != "VTODO":
                    continue
                due = comp.get("DUE")
                due_dt = getattr(due, "dt", None) if due is not None else None
                if isinstance(due_dt, date) and not isinstance(due_dt, datetime):
                    due_dt = datetime(due_dt.year, due_dt.month, due_dt.day, tzinfo=timezone.utc)
                out.append(
                    ReminderIn(
                        title=str(comp.get("SUMMARY", "(без названия)")),
                        notes=str(comp.get("DESCRIPTION")) if comp.get("DESCRIPTION") else None,
                        list=cal.get_display_name() or "",
                        due=due_dt,
                        priority=int(comp.get("PRIORITY", 0) or 0),
                        completed=str(comp.get("STATUS", "")).upper() == "COMPLETED",
                        url=str(comp.get("URL")) if comp.get("URL") else None,
                    )
                )
    return out


async def sync_caldav(db: AsyncSession) -> int:
    if not icloud.enabled():
        return 0
    items = await asyncio.to_thread(_caldav_todos_sync)
    return await apply_snapshot(db, items, source="caldav")
