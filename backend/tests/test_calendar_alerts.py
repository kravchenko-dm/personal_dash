"""Calendar alerts: reminders before events, and new/changed/removed detection."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal
from app.services import agenda, calendar_alerts, push, telegram_bot

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)


def ev(eid: str, title: str, start: datetime, minutes: int = 60, **extra) -> dict:
    return {
        "id": eid,
        "calendar_id": "primary",
        "calendar": "Личный",
        "source": "google",
        "title": title,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "all_day": False,
        "location": extra.get("location"),
    }


@pytest.fixture
def fake(monkeypatch):
    """Patch providers and sinks; returns a dict that tests mutate between ticks."""
    world = {"events": [], "errors": [], "push": [], "tg": []}

    async def events(db, days=7, start=None):
        return list(world["events"]), list(world["errors"])

    async def connected(db):
        return True

    async def notify_all(db, title, body, url="/", tag=None):
        world["push"].append((title, body, tag))
        return 1

    async def send_to_owner(text, reply_markup=None):
        world["tg"].append(text)

    monkeypatch.setattr(agenda, "events", events)
    monkeypatch.setattr(agenda, "connected", connected)
    monkeypatch.setattr(push, "notify_all", notify_all)
    monkeypatch.setattr(telegram_bot, "send_to_owner", send_to_owner)
    return world


async def test_reminder_sent_once(fake):
    fake["events"] = [
        ev("a", "Созвон", NOW + timedelta(minutes=10), location="Zoom"),
        ev("b", "Позже", NOW + timedelta(hours=3)),
        {
            **ev("c", "Весь день", NOW + timedelta(minutes=5)),
            "all_day": True,
            "start": "2026-11-08",
            "end": "2026-11-09",
        },
    ]
    async with SessionLocal() as db:
        stats = await calendar_alerts.tick(db, now=NOW)
        assert stats["reminded"] == 1
        assert fake["push"][0][0] == "Через 10 мин: Созвон" and "Zoom" in fake["push"][0][1]
        assert fake["tg"] and fake["tg"][0].startswith("📅 Через 10 мин: Созвон")
        # same tick again: no duplicate
        stats = await calendar_alerts.tick(db, now=NOW + timedelta(minutes=2))
        assert stats["reminded"] == 0 and len(fake["push"]) == 1
        # the event moved: a fresh reminder is allowed
        fake["events"][0] = ev("a", "Созвон", NOW + timedelta(minutes=12))
        stats = await calendar_alerts.tick(db, now=NOW + timedelta(minutes=2))
        assert stats["reminded"] == 1 and stats["changed"] == 1


async def test_change_detection(fake):
    base = NOW + timedelta(days=1)
    fake["events"] = [ev("a", "Встреча", base), ev("b", "Обед", base + timedelta(hours=3))]
    async with SessionLocal() as db:
        # first run only takes a snapshot
        stats = await calendar_alerts.tick(db, now=NOW)
        assert stats["added"] == stats["changed"] == stats["removed"] == 0 and not fake["tg"]

        fake["events"] = [
            ev("a", "Встреча", base + timedelta(hours=1)),  # moved
            ev("c", "Новая", base + timedelta(hours=5)),  # added within the old horizon
            ev("far", "Через неделю", NOW + timedelta(days=7, hours=2)),  # scrolled into view: not new
        ]  # "b" removed
        stats = await calendar_alerts.tick(db, now=NOW + timedelta(minutes=5))
        assert (stats["added"], stats["changed"], stats["removed"]) == (1, 1, 1)
        msg = fake["tg"][-1]
        assert (
            "➕ Новая" in msg
            and "✏️ Встреча" in msg
            and "было 09.11 12:00" in msg
            and "❌ Отменено: Обед" in msg
        )
        tags = {p[2] for p in fake["push"]}
        assert {"cal-new-c", "cal-chg-a", "cal-del-b"} <= tags

        # a provider error must not look like mass cancellation
        fake["events"], fake["errors"] = [], ["google: boom"]
        stats = await calendar_alerts.tick(db, now=NOW + timedelta(minutes=10))
        assert stats["skipped"] is True and stats["removed"] == 0


async def test_past_events_are_not_cancellations(fake):
    fake["events"] = [ev("a", "Утро", NOW + timedelta(minutes=30))]
    async with SessionLocal() as db:
        await calendar_alerts.tick(db, now=NOW)
        fake["events"] = []  # it already happened and fell out of the window
        stats = await calendar_alerts.tick(db, now=NOW + timedelta(hours=2))
        assert stats["removed"] == 0


async def test_disabled_does_nothing(fake):
    fake["events"] = [ev("a", "Созвон", NOW + timedelta(minutes=5))]
    async with SessionLocal() as db:
        await calendar_alerts.set_config(db, {"remind_minutes": 0, "watch_changes": False})
        stats = await calendar_alerts.tick(db, now=NOW)
        assert stats["reminded"] == 0 and not fake["push"]


async def test_alerts_endpoint(client, auth):
    r = await client.get("/api/calendar/alerts", headers=auth)
    assert r.status_code == 200 and r.json()["remind_minutes"] == 15 and r.json()["watch_changes"] is True
    r = await client.put(
        "/api/calendar/alerts",
        json={"remind_minutes": 30, "watch_changes": False, "watch_days": 3},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["remind_minutes"] == 30
    r = await client.get("/api/calendar/alerts", headers=auth)
    assert r.json() == {
        "remind_minutes": 30,
        "watch_changes": False,
        "watch_days": 3,
        "choices": [0, 5, 10, 15, 30, 60, 120],
    }
    r = await client.put("/api/calendar/alerts", json={"remind_minutes": 7}, headers=auth)
    assert r.status_code == 400
