"""Google Calendar: OAuth (web flow) + reading events. All googleapiclient calls run in a thread."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import jwt
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import OAuthToken, Setting

log = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
PROVIDER = "google"


def _client_config() -> dict:
    s = get_settings()
    return {
        "web": {
            "client_id": s.google_client_id,
            "client_secret": s.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri()],
        }
    }


def redirect_uri() -> str:
    return f"{get_settings().public_url}/api/calendar/oauth/callback"


def _state() -> str:
    return jwt.encode({"exp": int(time.time()) + 600, "p": PROVIDER}, get_settings().app_secret, "HS256")


def _check_state(state: str) -> bool:
    try:
        jwt.decode(state, get_settings().app_secret, algorithms=["HS256"])
        return True
    except jwt.PyJWTError:
        return False


def auth_url() -> str:
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri())
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true", state=_state()
    )
    return url


async def finish_oauth(db: AsyncSession, code: str, state: str) -> None:
    if not _check_state(state):
        raise ValueError("Неверный state")
    flow = Flow.from_client_config(_client_config(), scopes=SCOPES, redirect_uri=redirect_uri())
    await asyncio.to_thread(flow.fetch_token, code=code)
    creds = flow.credentials
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }
    row = await db.get(OAuthToken, PROVIDER)
    if row:
        if not data["refresh_token"]:
            data["refresh_token"] = row.data.get("refresh_token")
        row.data = data
    else:
        db.add(OAuthToken(provider=PROVIDER, data=data))
    await db.commit()


async def is_connected(db: AsyncSession) -> bool:
    row = await db.get(OAuthToken, PROVIDER)
    return bool(row and row.data.get("refresh_token"))


async def disconnect(db: AsyncSession) -> None:
    row = await db.get(OAuthToken, PROVIDER)
    if row:
        await db.delete(row)
        await db.commit()


async def _credentials(db: AsyncSession) -> Credentials:
    row = await db.get(OAuthToken, PROVIDER)
    if not row:
        raise RuntimeError("Google Calendar не подключён")
    creds = Credentials(**row.data)
    if not creds.valid:
        await asyncio.to_thread(creds.refresh, GoogleRequest())
        row.data = {**row.data, "token": creds.token}
        await db.commit()
    return creds


def _service(creds: Credentials):
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


async def list_calendars(db: AsyncSession) -> list[dict]:
    creds = await _credentials(db)

    def _run():
        items = _service(creds).calendarList().list().execute().get("items", [])
        return [
            {
                "id": c["id"],
                "summary": c.get("summaryOverride") or c.get("summary", ""),
                "color": c.get("backgroundColor"),
                "primary": c.get("primary", False),
                "source": "google",
            }
            for c in items
        ]

    return await asyncio.to_thread(_run)


async def selected_calendars(db: AsyncSession) -> list[str]:
    row = await db.get(Setting, "calendar.selected")
    return list(row.value.get("ids", [])) if row else []


async def set_selected_calendars(db: AsyncSession, ids: list[str]) -> None:
    row = await db.get(Setting, "calendar.selected")
    if row:
        row.value = {"ids": ids}
    else:
        db.add(Setting(key="calendar.selected", value={"ids": ids}))
    await db.commit()


async def list_events(db: AsyncSession, days: int = 7, start: datetime | None = None) -> list[dict]:
    creds = await _credentials(db)
    ids = await selected_calendars(db) or ["primary"]
    time_min = (start or datetime.now(timezone.utc)).isoformat()
    time_max = ((start or datetime.now(timezone.utc)) + timedelta(days=days)).isoformat()

    def _run():
        service = _service(creds)
        events: list[dict] = []
        names = {c["id"]: c["summary"] for c in service.calendarList().list().execute().get("items", [])}
        for cal_id in ids:
            resp = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                )
                .execute()
            )
            for e in resp.get("items", []):
                start_ = e.get("start", {})
                end_ = e.get("end", {})
                events.append(
                    {
                        "id": e["id"],
                        "calendar_id": cal_id,
                        "calendar": names.get(cal_id, ""),
                        "source": "google",
                        "title": e.get("summary", "(без названия)"),
                        "start": start_.get("dateTime") or start_.get("date"),
                        "end": end_.get("dateTime") or end_.get("date"),
                        "all_day": "date" in start_,
                        "location": e.get("location"),
                        "link": e.get("htmlLink"),
                        "description": (e.get("description") or "")[:500],
                    }
                )
        events.sort(key=lambda x: x["start"])
        return events

    return await asyncio.to_thread(_run)
