"""Merged agenda across calendar providers (Google + iCloud)."""

import asyncio
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import calendar as google
from app.services import icloud

log = logging.getLogger(__name__)


async def events(
    db: AsyncSession, days: int = 7, start: datetime | None = None
) -> tuple[list[dict], list[str]]:
    """Returns (events sorted by start, provider errors)."""
    sources: list[tuple[str, object]] = []
    if await google.is_connected(db):
        sources.append(("google", google.list_events(db, days=days, start=start)))
    if icloud.enabled():
        sources.append(("icloud", icloud.list_events(db, days=days, start=start)))
    results = await asyncio.gather(*(c for _, c in sources), return_exceptions=True)
    merged: list[dict] = []
    errors: list[str] = []
    for (name, _), result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("%s calendar failed: %s", name, result)
            errors.append(f"{name}: {result}")
            continue
        for e in result:
            e.setdefault("source", name)
            merged.append(e)
    merged.sort(key=lambda e: e["start"])
    return merged, errors


async def connected(db: AsyncSession) -> bool:
    return icloud.enabled() or await google.is_connected(db)


async def sources(db: AsyncSession) -> list[str]:
    """Names of the providers that are currently connected."""
    out: list[str] = []
    if await google.is_connected(db):
        out.append("google")
    if icloud.enabled():
        out.append("icloud")
    return out


async def upcoming_text(db: AsyncSession, days: int = 3) -> str:
    if not await connected(db):
        return ""
    found, _ = await events(db, days=days)
    if not found:
        return "Событий нет."
    lines = []
    for e in found[:40]:
        when = e["start"][:16].replace("T", " ") if not e["all_day"] else e["start"] + " (весь день)"
        cal = f" [{e['calendar']}]" if e.get("calendar") else ""
        lines.append(f"- {when}: {e['title']}{cal}")
    return "\n".join(lines)
