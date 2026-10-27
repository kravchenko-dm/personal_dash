from datetime import datetime, timezone

import icalendar

from app.services import icloud

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:abc-1
SUMMARY:Стоматолог Маши
DTSTART:20261028T090000Z
DTEND:20261028T100000Z
LOCATION:Клиника
END:VEVENT
BEGIN:VEVENT
UID:abc-2
SUMMARY:День рождения
DTSTART;VALUE=DATE:20261030
DTEND;VALUE=DATE:20261031
END:VEVENT
BEGIN:VEVENT
UID:abc-3
SUMMARY:Отменено
STATUS:CANCELLED
DTSTART:20261028T120000Z
END:VEVENT
END:VCALENDAR
"""


class FakeItem:
    def __init__(self, comp):
        self.icalendar_component = comp
        self.url = "https://caldav/x.ics"


class FakeCalendar:
    url = "https://caldav.icloud.com/123/calendars/family/"

    def get_display_name(self):
        return "Семья"

    def search(self, **kw):
        cal = icalendar.Calendar.from_ical(ICS)
        return [FakeItem(c) for c in cal.walk("VEVENT")]


class FakePrincipal:
    def calendars(self):
        return [FakeCalendar()]


class FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def principal(self):
        return FakePrincipal()


async def test_icloud_events_parsed(monkeypatch):
    monkeypatch.setattr(icloud, "_client", lambda: FakeClient())
    monkeypatch.setattr(icloud, "enabled", lambda: True)
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    events = icloud._events_sync([], start, start)
    assert [e["title"] for e in events] == ["Стоматолог Маши", "День рождения"]
    timed, allday = events
    assert timed["all_day"] is False and timed["location"] == "Клиника" and timed["source"] == "icloud"
    assert allday["all_day"] is True and allday["start"] == "2026-10-30"
    assert timed["calendar"] == "Семья"


async def test_calendars_endpoint_merges_sources(client, auth, monkeypatch):
    monkeypatch.setattr(icloud, "_client", lambda: FakeClient())
    monkeypatch.setattr(icloud, "enabled", lambda: True)
    resp = await client.get("/api/calendar/calendars", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["calendars"][0]["summary"] == "Семья" and body["calendars"][0]["source"] == "icloud"
    assert body["selected"] == {"google": [], "icloud": []}

    put = await client.put(
        "/api/calendar/calendars/selected", json={"ids": [FakeCalendar.url], "source": "icloud"}, headers=auth
    )
    assert put.status_code == 200
    body = (await client.get("/api/calendar/calendars", headers=auth)).json()
    assert body["selected"]["icloud"] == [FakeCalendar.url]

    events = (await client.get("/api/calendar/events?days=7", headers=auth)).json()
    assert events["connected"] is True and len(events["events"]) == 2 and events["errors"] == []


async def test_events_not_connected(client, auth):
    body = (await client.get("/api/calendar/events", headers=auth)).json()
    assert body == {"connected": False, "events": [], "errors": []}
