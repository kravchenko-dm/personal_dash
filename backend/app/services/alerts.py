"""Incidents: open/resolve with dedupe, notify via push + Telegram."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident, Project
from app.services import push, telegram_bot
from app.utils import aware

log = logging.getLogger(__name__)
ICON = {"warning": "⚠️", "critical": "🔴"}


async def open_incident(
    db: AsyncSession,
    project_id: int,
    kind: str,
    key: str,
    message: str,
    severity: str = "critical",
    server_id: int | None = None,
    monitor_id: int | None = None,
) -> Incident | None:
    """Returns the new incident, or None when one with the same (project, kind, key) is already open."""
    existing = await db.scalar(
        select(Incident).where(
            Incident.project_id == project_id,
            Incident.kind == kind,
            Incident.key == key,
            Incident.resolved_at.is_(None),
        )
    )
    if existing:
        return None
    incident = Incident(
        project_id=project_id,
        kind=kind,
        key=key,
        message=message,
        severity=severity,
        server_id=server_id,
        monitor_id=monitor_id,
    )
    db.add(incident)
    await db.flush()
    project = await db.get(Project, project_id)
    name = project.name if project else f"#{project_id}"
    await _notify(
        db, f"{ICON.get(severity, '•')} {name}: {message}", f"/projects/{project_id}", f"inc-{incident.id}"
    )
    incident.notified = True
    return incident


async def resolve_incident(db: AsyncSession, project_id: int, kind: str, key: str, note: str = "") -> bool:
    incident = await db.scalar(
        select(Incident).where(
            Incident.project_id == project_id,
            Incident.kind == kind,
            Incident.key == key,
            Incident.resolved_at.is_(None),
        )
    )
    if not incident:
        return False
    incident.resolved_at = datetime.now(timezone.utc)
    minutes = int((incident.resolved_at - aware(incident.started_at)).total_seconds() // 60)
    project = await db.get(Project, project_id)
    name = project.name if project else f"#{project_id}"
    await _notify(
        db,
        f"✅ {name}: {note or incident.message} — восстановлено через {minutes} мин",
        f"/projects/{project_id}",
        f"inc-{incident.id}",
    )
    return True


async def _notify(db: AsyncSession, text: str, url: str, tag: str) -> None:
    try:
        await push.notify_all(db, "Мониторинг", text, url=url, tag=tag)
    except Exception as exc:
        log.warning("push alert failed: %s", exc)
    try:
        await telegram_bot.send_to_owner(text)
    except Exception as exc:
        log.warning("telegram alert failed: %s", exc)


async def open_incidents(db: AsyncSession, project_id: int | None = None) -> list[Incident]:
    stmt = select(Incident).where(Incident.resolved_at.is_(None)).order_by(Incident.started_at.desc())
    if project_id is not None:
        stmt = stmt.where(Incident.project_id == project_id)
    return list((await db.scalars(stmt)).all())
