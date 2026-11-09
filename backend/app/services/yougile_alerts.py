"""YouGile alerts: a reminder before a timed deadline, notifications when tasks appear, get assigned
to the owner, get completed or have their deadline moved, and a block for the morning brief.

Settings: `settings` key `yougile.alerts` (editable from the UI); runtime state (snapshot of tasks,
sent reminders) in `yougile.alerts_state`. The owner is the API-key's user (`/users/me`); "mine"
means tasks assigned to the owner or created by them. Changes made by the owner are not announced.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Setting
from app.services import push, telegram_bot
from app.services import yougile as yg

log = logging.getLogger(__name__)

CONFIG_KEY = "yougile.alerts"
STATE_KEY = "yougile.alerts_state"
SCOPES = ("all", "mine")
REMIND_CHOICES = (0, 15, 30, 60, 120, 180)
DEFAULTS: dict[str, Any] = {"scope": "all", "remind_minutes": 60, "watch_changes": True, "brief": True}
PUSH_EACH_LIMIT = 3  # more changes than this in one pass -> one summary push
LOG_LOOKUPS = 10  # task-log requests per pass (API limit is 50/min per company)


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
    if cfg["scope"] not in SCOPES:
        raise ValueError(f"scope должно быть одним из {SCOPES}")
    try:
        cfg["remind_minutes"] = int(cfg["remind_minutes"])
    except (TypeError, ValueError):
        cfg["remind_minutes"] = DEFAULTS["remind_minutes"]
    if cfg["remind_minutes"] not in REMIND_CHOICES:
        raise ValueError(f"remind_minutes должно быть одним из {REMIND_CHOICES}")
    cfg["watch_changes"] = bool(cfg["watch_changes"])
    cfg["brief"] = bool(cfg["brief"])
    return cfg


async def get_config(db: AsyncSession) -> dict:
    return normalize_config(await _get(db, CONFIG_KEY))


async def set_config(db: AsyncSession, raw: dict) -> dict:
    cfg = normalize_config(raw)
    await _set(db, CONFIG_KEY, cfg)
    return cfg


# ---------- data ----------


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().tz)
    except Exception:
        return ZoneInfo("UTC")


def _deadline(t: dict) -> tuple[int | None, bool]:
    d = t.get("deadline") or {}
    return d.get("deadline"), bool(d.get("withTime"))


def _deadline_date(ts: int, with_time: bool) -> date:
    # all-day deadlines are stored as 12:00 UTC of that day
    moment = datetime.fromtimestamp(ts / 1000, timezone.utc)
    return moment.astimezone(_tz()).date() if with_time else moment.date()


def _fmt_deadline(ts: int | None, with_time: bool) -> str:
    if not ts:
        return ""
    if with_time:
        return datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(_tz()).strftime("%d.%m %H:%M")
    return _deadline_date(ts, False).strftime("%d.%m")


async def _load() -> tuple[str, list[dict], dict[str, str], dict[str, str]]:
    """(owner id, live tasks, task id -> project title, user id -> name)."""
    owner = (await yg.me())["id"]
    tasks = [t for t in await yg.tasks() if not t.get("deleted") and not t.get("archived")]
    cols = {c["id"]: c for c in await yg.columns()}
    boards = {b["id"]: b for b in await yg.boards()}
    projects = {p["id"]: (p.get("title") or "").strip() for p in await yg.projects()}
    names = {u["id"]: u.get("realName") or u.get("email", "") for u in await yg.users()}
    where: dict[str, str] = {}
    for t in tasks:
        board = boards.get((cols.get(t.get("columnId")) or {}).get("boardId"))
        if board:
            where[t["id"]] = projects.get(board.get("projectId"), "") or board.get("title", "")
    return owner, tasks, where, names


def _short(title: str, limit: int = 100) -> str:
    """One line, at most `limit` chars: YouGile titles are often whole paragraphs."""
    text = " ".join((title or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _in_scope(t: dict, owner: str, scope: str) -> bool:
    return scope == "all" or owner in (t.get("assigned") or []) or t.get("createdBy") == owner


def _describe(t: dict, where: dict[str, str], names: dict[str, str]) -> str:
    parts = []
    if where.get(t["id"]):
        parts.append(where[t["id"]])
    ts, with_time = _deadline(t)
    if ts:
        parts.append(f"срок {_fmt_deadline(ts, with_time)}")
    people = [names.get(u, "?") for u in (t.get("assigned") or [])]
    if people:
        parts.append(", ".join(people))
    return " · ".join(parts)


def _entry(t: dict) -> dict:
    ts, with_time = _deadline(t)
    return {
        "title": _short(t.get("title", "")),
        "completed": bool(t.get("completed")),
        "deadline": ts,
        "with_time": with_time,
        "assigned": sorted(t.get("assigned") or []),
        "created_by": t.get("createdBy"),
    }


def _deadline_moved_by(t: dict) -> str | None:
    history = (t.get("deadline") or {}).get("history") or []
    return history[-1].get("by") if history else None


# ---------- the tick ----------


async def tick(db: AsyncSession, now: datetime | None = None) -> dict:
    """One pass. Returns counters for logging/tests. Raises if YouGile is unreachable (state kept)."""
    now = now or datetime.now(timezone.utc)
    stats = {"reminded": 0, "added": 0, "assigned": 0, "completed": 0, "moved": 0}
    if not get_settings().yougile_api_key:
        return stats
    cfg = await get_config(db)
    if not (cfg["remind_minutes"] or cfg["watch_changes"]):
        return stats

    owner, tasks, where, names = await _load()
    state = await _get(db, STATE_KEY)
    now_ms = int(now.timestamp() * 1000)
    pushes: list[tuple[str, str, str]] = []  # (title, body, tag)
    lines: list[str] = []

    # 1) reminders before timed deadlines ------------------------------------------------------
    reminded: dict[str, int] = dict(state.get("reminded") or {})
    if cfg["remind_minutes"]:
        horizon = now_ms + cfg["remind_minutes"] * 60_000
        for t in tasks:
            ts, with_time = _deadline(t)
            if not (ts and with_time) or t.get("completed") or not _in_scope(t, owner, cfg["scope"]):
                continue
            if not (now_ms < ts <= horizon) or reminded.get(t["id"]) == ts:
                continue
            minutes = max(1, round((ts - now_ms) / 60_000))
            left = f"{minutes} мин" if minutes < 60 else f"{minutes / 60:.0f} ч"
            title = f"Через {left} срок: {_short(t.get('title', ''), 80)}"
            body = _describe(t, where, names)
            await push.notify_all(db, title, body, url="/yougile", tag=f"ygl-{t['id']}")
            await telegram_bot.send_to_owner(f"🗂 {title}\n{body}")
            reminded[t["id"]] = ts
            stats["reminded"] += 1
    cutoff = now_ms - 2 * 86_400_000
    reminded = {k: v for k, v in reminded.items() if v >= cutoff}

    # 2) changes since the previous pass ---------------------------------------------------------
    snapshot: dict[str, dict] = dict(state.get("snapshot") or {})
    if cfg["watch_changes"] and state.get("snapshot") is not None:
        log_budget = [LOG_LOOKUPS]

        async def actor(tid: str, kind: str) -> str | None:
            """Who did the latest `kind` action on a task (None if unknown or over budget)."""
            if log_budget[0] <= 0:
                return None
            log_budget[0] -= 1
            try:
                entry = next((e for e in await yg.task_log(tid) if e.get("type") == kind), None)
            except Exception as exc:
                log.info("yougile log %s: %s", tid, exc)
                return None
            return entry.get("userId") if entry else None

        def by(uid: str | None) -> str:
            return f" ({names[uid]})" if uid and names.get(uid) else ""

        for t in tasks:
            tid, title = t["id"], _short(t.get("title", ""))
            desc = _describe(t, where, names)
            old = snapshot.get(tid)
            mine_now = owner in (t.get("assigned") or [])
            if old is None:
                if t.get("completed") or t.get("createdBy") == owner:
                    continue  # created by the owner (here or in YouGile) or already done
                author = by(t.get("createdBy"))
                if mine_now:
                    lines.append(f"👤 Новая задача на вас: {title}{author}\n   {desc}")
                    pushes.append((f"На вас: {title}", desc, f"ygl-new-{tid}"))
                    stats["assigned"] += 1
                elif _in_scope(t, owner, cfg["scope"]):
                    lines.append(f"➕ {title}{author}\n   {desc}")
                    pushes.append((f"Новая задача: {title}", desc, f"ygl-new-{tid}"))
                    stats["added"] += 1
                continue
            if t.get("completed"):
                if not old.get("completed") and _in_scope(t, owner, cfg["scope"]):
                    who = await actor(tid, "done")
                    if who == owner:
                        continue
                    lines.append(f"✅ Выполнено: {title}{by(who)}\n   {desc}")
                    pushes.append((f"Выполнено: {title}", desc, f"ygl-done-{tid}"))
                    stats["completed"] += 1
                continue
            if mine_now and owner not in (old.get("assigned") or []):
                who = await actor(tid, "executor")
                if who != owner:
                    lines.append(f"👤 Назначена на вас: {title}{by(who)}\n   {desc}")
                    pushes.append((f"На вас: {title}", desc, f"ygl-asg-{tid}"))
                    stats["assigned"] += 1
                continue
            ts, with_time = _deadline(t)
            moved_by = _deadline_moved_by(t)
            if (
                ts
                and old.get("deadline")
                and ts != old["deadline"]
                and moved_by != owner
                and _in_scope(t, owner, cfg["scope"])
            ):
                was = _fmt_deadline(old["deadline"], bool(old.get("with_time")))
                lines.append(f"📆 Срок перенесён: {title}{by(moved_by)}\n   {desc} (было {was})")
                pushes.append((f"Срок перенесён: {title}", f"{desc} (было {was})", f"ygl-mv-{tid}"))
                stats["moved"] += 1

    if pushes:
        if len(pushes) <= PUSH_EACH_LIMIT:
            for title, body, tag in pushes:
                await push.notify_all(db, title, body, url="/yougile", tag=tag)
        else:
            body = "; ".join(p[0] for p in pushes[:4]) + ("…" if len(pushes) > 4 else "")
            await push.notify_all(
                db, f"YouGile: {len(pushes)} изменений", body, url="/yougile", tag="ygl-batch"
            )
    if lines:
        await telegram_bot.send_to_owner("🗂 YouGile, изменения:\n\n" + "\n".join(lines[:20]))

    await _set(
        db,
        STATE_KEY,
        {
            "reminded": reminded,
            "snapshot": {t["id"]: _entry(t) for t in tasks},
            "checked_at": now.isoformat(),
        },
    )
    return stats


# ---------- morning brief ----------


async def brief_text(db: AsyncSession, today: date | None = None, limit: int = 12) -> str:
    """Open tasks that are overdue / due today / due tomorrow, for the morning brief. '' if none."""
    if not get_settings().yougile_api_key:
        return ""
    cfg = await get_config(db)
    if not cfg["brief"]:
        return ""
    owner, tasks, where, names = await _load()
    today = today or datetime.now(_tz()).date()
    groups: dict[str, list[tuple[date, str]]] = {"overdue": [], "today": [], "tomorrow": []}
    for t in tasks:
        ts, with_time = _deadline(t)
        if not ts or t.get("completed") or not _in_scope(t, owner, cfg["scope"]):
            continue
        day = _deadline_date(ts, with_time)
        key = (
            "overdue"
            if day < today
            else "today"
            if day == today
            else "tomorrow"
            if day == today + timedelta(days=1)
            else None
        )
        if not key:
            continue
        extra = [where.get(t["id"])] if where.get(t["id"]) else []
        if key == "overdue":
            extra.append(f"срок {_fmt_deadline(ts, with_time)}")
        elif with_time:
            extra.append(_fmt_deadline(ts, True)[-5:])
        people = [names.get(u, "?") for u in (t.get("assigned") or []) if u != owner]
        if owner in (t.get("assigned") or []):
            people.insert(0, "вы")
        if people:
            extra.append(", ".join(people))
        groups[key].append(
            (day, f"• {_short(t.get('title', ''))}" + (f" — {' · '.join(extra)}" if extra else ""))
        )
    out: list[str] = []
    titles = {"overdue": "⚠️ Просрочено", "today": "Сегодня", "tomorrow": "Завтра"}
    room = limit
    for key, items in groups.items():
        if not items:
            continue
        items.sort(key=lambda x: x[0])
        out.append(f"{titles[key]} ({len(items)}):")
        out.extend(line for _, line in items[:room])
        if len(items) > room:
            out.append(f"  … ещё {len(items) - room}")
        room = max(0, room - len(items))
    return "\n".join(out)
