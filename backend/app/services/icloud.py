"""iCloud (Apple) calendars over CalDAV, including the shared Family calendar.

Auth: Apple ID + app-specific password (appleid.apple.com -> Sign-In and Security ->
App-Specific Passwords). All caldav calls are blocking and run in a thread."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import caldav
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Setting

log = logging.getLogger(__name__)
CALDAV_URL = "https://caldav.icloud.com/"
SELECTED_KEY = "calendar.icloud_selected"


def enabled() -> bool:
    s = get_settings()
    return bool(s.icloud_username and s.icloud_app_password)


def _client() -> caldav.DAVClient:
    s = get_settings()
    return caldav.DAVClient(
        url=CALDAV_URL, username=s.icloud_username, password=s.icloud_app_password, timeout=30
    )


def _calendars_sync() -> list[caldav.Calendar]:
    with _client() as client:
        return client.principal().calendars()


async def list_calendars() -> list[dict]:
    cals = await asyncio.to_thread(_calendars_sync)
    out = []
    for c in cals:
        name = c.get_display_name() or str(c.url)
        out.append({"id": str(c.url), "summary": name, "color": None, "primary": False, "source": "icloud"})
    return out


async def selected_calendars(db: AsyncSession) -> list[str]:
    row = await db.get(Setting, SELECTED_KEY)
    return list(row.value.get("ids", [])) if row else []


async def set_selected_calendars(db: AsyncSession, ids: list[str]) -> None:
    row = await db.get(Setting, SELECTED_KEY)
    if row:
        row.value = {"ids": ids}
    else:
        db.add(Setting(key=SELECTED_KEY, value={"ids": ids}))
    await db.commit()


def _iso(value) -> tuple[str, bool]:
    """icalendar DTSTART/DTEND -> (iso string, all_day)."""
    dt = getattr(value, "dt", value)
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(), False
    if isinstance(dt, date):
        return dt.isoformat(), True
    return str(dt), False


def _events_sync(ids: list[str], start: datetime, end: datetime) -> list[dict]:
    events: list[dict] = []
    with _client() as client:
        cals = client.principal().calendars()
        wanted = [c for c in cals if not ids or str(c.url) in ids]
        for cal in wanted:
            name = cal.get_display_name() or ""
            try:
                found = cal.search(start=start, end=end, event=True, expand=True)
            except Exception as exc:
                log.warning("icloud calendar %s search failed: %s", name, exc)
                continue
            for item in found:
                comp = item.icalendar_component
                if comp is None or comp.name != "VEVENT":
                    continue
                if str(comp.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                dtstart = comp.get("DTSTART")
                if dtstart is None:
                    continue
                start_iso, all_day = _iso(dtstart)
                dtend = comp.get("DTEND")
                end_iso = _iso(dtend)[0] if dtend is not None else start_iso
                uid = str(comp.get("UID", item.url))
                rid = comp.get("RECURRENCE-ID")
                events.append(
                    {
                        "id": f"{uid}:{_iso(rid)[0]}" if rid is not None else uid,
                        "calendar_id": str(cal.url),
                        "calendar": name,
                        "source": "icloud",
                        "title": str(comp.get("SUMMARY", "(без названия)")),
                        "start": start_iso,
                        "end": end_iso,
                        "all_day": all_day,
                        "location": str(comp.get("LOCATION")) if comp.get("LOCATION") else None,
                        "link": str(comp.get("URL")) if comp.get("URL") else None,
                        "description": str(comp.get("DESCRIPTION", ""))[:500],
                    }
                )
    return events


async def list_events(db: AsyncSession, days: int = 7, start: datetime | None = None) -> list[dict]:
    if not enabled():
        return []
    ids = await selected_calendars(db)
    begin = start or datetime.now(timezone.utc)
    return await asyncio.to_thread(_events_sync, ids, begin, begin + timedelta(days=days))
