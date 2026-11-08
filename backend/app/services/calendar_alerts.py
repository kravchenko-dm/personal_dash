"""Calendar alerts: a reminder shortly before an event starts, and notifications when events in the
coming days appear, move or disappear. Works on the merged agenda (Google + iCloud).

Settings live in the `settings` table (key `calendar.alerts`) so they are editable from the UI;
runtime state (what was already reminded, last snapshot) is in `calendar.alerts_state`.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Setting
from app.services import agenda, push, telegram_bot

log = logging.getLogger(__name__)

CONFIG_KEY = "calendar.alerts"
STATE_KEY = "calendar.alerts_state"
DEFAULTS: dict[str, Any] = {"remind_minutes": 15, "watch_changes": True, "watch_days": 7}
REMIND_CHOICES = (0, 5, 10, 15, 30, 60, 120)
# fields whose change counts as "the event changed"
_TRACKED = ("title", "start", "end", "location", "calendar")


# ---------- settings ----------


async def _get(db: AsyncSession, key: str) -> dict:
    row = await db.get(Setting, key)
    return dict(row.value) if row else {}


async def _set(db: AsyncSession, key: str, value: dict) -> None:
    row = await db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    await db.commit()


def normalize_config(raw: dict) -> dict:
    cfg = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    try:
        cfg["remind_minutes"] = int(cfg["remind_minutes"])
    except (TypeError, ValueError):
        cfg["remind_minutes"] = DEFAULTS["remind_minutes"]
    if cfg["remind_minutes"] not in REMIND_CHOICES:
        raise ValueError(f"remind_minutes должно быть одним из {REMIND_CHOICES}")
    cfg["watch_changes"] = bool(cfg["watch_changes"])
    try:
        cfg["watch_days"] = max(1, min(30, int(cfg["watch_days"])))
    except (TypeError, ValueError):
        cfg["watch_days"] = DEFAULTS["watch_days"]
    return cfg


async def get_config(db: AsyncSession) -> dict:
    return normalize_config(await _get(db, CONFIG_KEY))


async def set_config(db: AsyncSession, raw: dict) -> dict:
    cfg = normalize_config(raw)
    await _set(db, CONFIG_KEY, cfg)
    return cfg


# ---------- formatting ----------


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:
        return ZoneInfo("UTC")


def _parse(value: str) -> datetime | date:
    if len(value) == 10:
        return date.fromisoformat(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_dt(value: str) -> datetime:
    """Start of an event as an aware datetime (all-day -> local midnight)."""
    parsed = _parse(value)
    if isinstance(parsed, datetime):
        return parsed
    return datetime.combine(parsed, datetime.min.time(), tzinfo=_tz())


def _when(e: dict) -> str:
    tz = _tz()
    if e.get("all_day"):
        d = _parse(e["start"])
        return f"{d.strftime('%d.%m')} (весь день)" if isinstance(d, date) else e["start"]
    start = _as_dt(e["start"]).astimezone(tz)
    text = start.strftime("%d.%m %H:%M")
    if e.get("end"):
        end = _as_dt(e["end"]).astimezone(tz)
        text += f"–{end.strftime('%H:%M')}"
    return text


def _describe(e: dict) -> str:
    parts = [_when(e)]
    if e.get("location"):
        parts.append(str(e["location"]))
    if e.get("calendar"):
        parts.append(f"[{e['calendar']}]")
    return " · ".join(parts)


def _snapshot_entry(e: dict) -> dict:
    entry = {k: e.get(k) for k in _TRACKED}
    entry["all_day"] = bool(e.get("all_day"))
    entry["source"] = e.get("source")
    return entry


# ---------- the tick ----------


async def tick(db: AsyncSession, now: datetime | None = None) -> dict:
    """Run one pass. Returns counters for logging/tests."""
    now = now or datetime.now(timezone.utc)
    cfg = await get_config(db)
    stats = {"reminded": 0, "added": 0, "changed": 0, "removed": 0, "skipped": False}
    if not (cfg["remind_minutes"] or cfg["watch_changes"]):
        return stats
    if not await agenda.connected(db):
        return stats

    state = await _get(db, STATE_KEY)
    events, errors = await agenda.events(db, days=cfg["watch_days"], start=now)
    by_id = {e["id"]: e for e in events if e.get("id")}
    sources = await agenda.sources(db)

    # 1) reminders -------------------------------------------------------------------------
    reminded: dict[str, str] = dict(state.get("reminded") or {})
    if cfg["remind_minutes"]:
        horizon = now + timedelta(minutes=cfg["remind_minutes"])
        for e in events:
            if e.get("all_day"):
                continue
            start = _as_dt(e["start"])
            if not (now < start <= horizon):
                continue
            if reminded.get(e["id"]) == e["start"]:
                continue
            minutes = max(1, round((start - now).total_seconds() / 60))
            title = f"Через {minutes} мин: {e['title']}"
            await push.notify_all(db, title, _describe(e), url="/calendar", tag=f"cal-{e['id']}")
            await telegram_bot.send_to_owner(f"📅 {title}\n{_describe(e)}")
            reminded[e["id"]] = e["start"]
            stats["reminded"] += 1
    # forget reminders for events that are long gone
    cutoff = now - timedelta(days=2)
    reminded = {k: v for k, v in reminded.items() if _as_dt(v) >= cutoff}

    # 2) changes ---------------------------------------------------------------------------
    snapshot: dict[str, dict] = dict(state.get("snapshot") or {})
    new_snapshot = {eid: _snapshot_entry(e) for eid, e in by_id.items()}
    horizon_iso = (now + timedelta(days=cfg["watch_days"])).isoformat()
    if cfg["watch_changes"]:
        if errors:
            # a provider failed: its events would all look "removed"; keep the old snapshot
            stats["skipped"] = True
            new_snapshot = snapshot
            horizon_iso = state.get("horizon") or horizon_iso
        elif snapshot or state.get("horizon"):
            prev_horizon = _as_dt(state["horizon"]) if state.get("horizon") else now
            # a provider connected since the last pass: its events are a baseline, not news
            known = set(state.get("sources") or {v.get("source") for v in snapshot.values()})
            fresh_sources = set(sources) - known
            lines: list[str] = []
            for eid, e in by_id.items():
                old = snapshot.get(eid)
                if old is None:
                    # events that merely scrolled into the window are not "new"
                    if _as_dt(e["start"]) > prev_horizon or e.get("source") in fresh_sources:
                        continue
                    lines.append(f"➕ {e['title']}\n   {_describe(e)}")
                    await push.notify_all(
                        db, f"Новое: {e['title']}", _describe(e), url="/calendar", tag=f"cal-new-{eid}"
                    )
                    stats["added"] += 1
                    continue
                diff = [k for k in _TRACKED if (old.get(k) or None) != (e.get(k) or None)]
                if diff:
                    was = _when({**old}) if "start" in diff or "end" in diff else None
                    note = f" (было {was})" if was else ""
                    lines.append(f"✏️ {e['title']}\n   {_describe(e)}{note}")
                    await push.notify_all(
                        db,
                        f"Изменено: {e['title']}",
                        _describe(e) + note,
                        url="/calendar",
                        tag=f"cal-chg-{eid}",
                    )
                    stats["changed"] += 1
            for eid, old in snapshot.items():
                if eid in by_id:
                    continue
                if _as_dt(old["start"]) <= now:
                    continue  # it simply happened already
                if old.get("source") and old["source"] not in sources:
                    continue  # the provider was disconnected, nothing was cancelled
                lines.append(f"❌ Отменено: {old['title']}\n   {_describe(old)}")
                await push.notify_all(
                    db, f"Отменено: {old['title']}", _describe(old), url="/calendar", tag=f"cal-del-{eid}"
                )
                stats["removed"] += 1
            if lines:
                await telegram_bot.send_to_owner("📅 Календарь, изменения:\n\n" + "\n".join(lines[:20]))
        # first run (no snapshot yet): just remember what we see

    await _set(
        db,
        STATE_KEY,
        {
            "reminded": reminded,
            "snapshot": new_snapshot,
            "horizon": horizon_iso,
            "sources": list(state.get("sources") or []) if stats["skipped"] else sources,
            "checked_at": now.isoformat(),
        },
    )
    return stats
