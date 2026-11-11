"""Servers (agent reports + command queue) and HTTP monitors."""

import asyncio
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentCommand, BackupPlan, CheckSample, MetricSample, Monitor, Server
from app.services import alerts
from app.utils import aware

log = logging.getLogger(__name__)
BACKUP_KINDS = {"backup", "backup_snapshots", "restore", "backup_check"}
BAD_SERVICE_STATES = {"failed", "stopped", "exited", "dead", "unhealthy", "missing", "restarting", "stale"}
# "idle" = scheduled job between runs, "running"/"starting" fine
RETENTION_DAYS = 30
# a service must stay bad this long before it is an incident: a deploy restarts containers and
# the health check refuses connections for a few seconds, which is not worth two notifications
SERVICE_GRACE = timedelta(minutes=2)


# ---------- tokens ----------


def new_token() -> str:
    return "dash_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def server_by_token(db: AsyncSession, token: str) -> Server | None:
    if not token:
        return None
    return await db.scalar(select(Server).where(Server.token_hash == hash_token(token)))


# ---------- agent reports ----------


def _worst_disk(metrics: dict[str, Any]) -> tuple[float, str]:
    worst, path = 0.0, "/"
    for d in metrics.get("disks") or []:
        if float(d.get("pct", 0)) > worst:
            worst, path = float(d["pct"]), d.get("path", "/")
    return worst, path


async def ingest_report(db: AsyncSession, server: Server, report: dict[str, Any]) -> list[AgentCommand]:
    now = datetime.now(timezone.utc)
    prev_restarts: dict[str, int] = dict((server.last_report or {}).get("_restarts", {}))
    prev_bad_since: dict[str, str] = dict((server.last_report or {}).get("_bad_since", {}))
    server.hostname = report.get("hostname") or server.hostname
    server.os = report.get("pretty_os") or report.get("os") or server.os
    server.agent_version = report.get("agent_version")
    server.last_seen_at = now
    services = report.get("services") or []
    # assigned as one new dict: autoflush fires on the first query below, so later in-place
    # mutations of a JSON column would be lost
    server.last_report = {
        "metrics": report.get("metrics") or {},
        "services": services,
        "failed_units": report.get("failed_units") or [],
        "python": report.get("python"),
        "_restarts": {s.get("name"): int(s.get("restarts") or 0) for s in services},
    }
    metrics = report.get("metrics") or {}
    disk_pct, disk_path = _worst_disk(metrics)
    db.add(
        MetricSample(
            server_id=server.id,
            ts=now,
            load1=float(metrics.get("load1", 0) or 0),
            mem_pct=float(metrics.get("mem_pct", 0) or 0),
            disk_pct=disk_pct,
        )
    )
    await _evaluate_server(db, server, disk_pct, disk_path, prev_restarts, prev_bad_since, now)
    await alerts.resolve_incident(
        db, server.project_id, "agent_silent", str(server.id), f"агент {server.name} снова на связи"
    )

    # hand out queued commands
    queued = list(
        (
            await db.scalars(
                select(AgentCommand)
                .where(AgentCommand.server_id == server.id, AgentCommand.status == "queued")
                .order_by(AgentCommand.id)
            )
        ).all()
    )
    for cmd in queued:
        cmd.status = "sent"
        cmd.sent_at = now
    await db.commit()
    return queued


async def _evaluate_server(
    db: AsyncSession,
    server: Server,
    disk_pct: float,
    disk_path: str,
    prev_restarts: dict[str, int],
    prev_bad_since: dict[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    pid = server.project_id
    now = now or datetime.now(timezone.utc)
    bad_since: dict[str, str] = {}
    # disk
    if disk_pct >= server.disk_alert_pct:
        await alerts.open_incident(
            db,
            pid,
            "disk_high",
            str(server.id),
            f"диск {disk_path} на {server.name} заполнен на {disk_pct:.0f}%",
            "warning",
            server_id=server.id,
        )
    else:
        await alerts.resolve_incident(
            db, pid, "disk_high", str(server.id), f"диск на {server.name} освободился ({disk_pct:.0f}%)"
        )
    # services
    for svc in (server.last_report or {}).get("services", []):
        key = f"{server.id}:{svc.get('name')}"
        status = str(svc.get("status", "unknown"))
        http = svc.get("http")
        bad = status in BAD_SERVICE_STATES or (http is not None and not http.get("ok"))
        if svc.get("discovered") and status in ("exited", "stopped", "dead"):
            bad = False  # unlisted stopped containers are usually intentional (one-off runs)
        if bad:
            name = str(svc.get("name"))
            since_iso = (prev_bad_since or {}).get(name) or now.isoformat()
            bad_since[name] = since_iso
            if now - datetime.fromisoformat(since_iso) < SERVICE_GRACE:
                continue  # maybe just a restart; restart-loop detection below is skipped for this pass
            reason = (
                status
                if status in BAD_SERVICE_STATES
                else f"HTTP {http.get('status') or http.get('error', '')}"
            )
            await alerts.open_incident(
                db,
                pid,
                "service_down",
                key,
                f"сервис {svc.get('name')} на {server.name}: {reason}",
                "critical",
                server_id=server.id,
            )
        else:
            await alerts.resolve_incident(
                db, pid, "service_down", key, f"сервис {svc.get('name')} на {server.name} работает"
            )
        restarts = int(svc.get("restarts") or 0)
        prev = prev_restarts.get(svc.get("name"))
        if prev is not None and restarts > prev + 2:
            await alerts.open_incident(
                db,
                pid,
                "restart_loop",
                key,
                f"сервис {svc.get('name')} на {server.name} перезапускается ({restarts} рестартов)",
                "warning",
                server_id=server.id,
            )
        elif prev is not None and restarts == prev:
            await alerts.resolve_incident(
                db, pid, "restart_loop", key, f"сервис {svc.get('name')} на {server.name} стабилен"
            )
    server.last_report = {
        **server.last_report,
        "_restarts": {
            s.get("name"): int(s.get("restarts") or 0) for s in server.last_report.get("services", [])
        },
        "_bad_since": bad_since,
    }
    # failed units anywhere on the host
    failed = set((server.last_report or {}).get("failed_units", []))
    for unit in failed:
        await alerts.open_incident(
            db,
            pid,
            "unit_failed",
            f"{server.id}:{unit}",
            f"юнит {unit} на {server.name} в состоянии failed",
            "warning",
            server_id=server.id,
        )
    open_units = [
        i
        for i in await alerts.open_incidents(db, pid)
        if i.kind == "unit_failed" and i.server_id == server.id
    ]
    for inc in open_units:
        unit = inc.key.split(":", 1)[1]
        if unit not in failed:
            await alerts.resolve_incident(
                db, pid, "unit_failed", inc.key, f"юнит {unit} на {server.name} восстановлен"
            )


async def check_silent_agents(db: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    servers = list((await db.scalars(select(Server))).all())
    for s in servers:
        seen = aware(s.last_seen_at)
        if seen is None:
            continue
        if now - seen > timedelta(minutes=s.silence_alert_min):
            minutes = int((now - seen).total_seconds() // 60)
            await alerts.open_incident(
                db,
                s.project_id,
                "agent_silent",
                str(s.id),
                f"агент {s.name} молчит {minutes} мин (сервер недоступен?)",
                "critical",
                server_id=s.id,
            )
    await db.commit()


async def finish_command(db: AsyncSession, cmd: AgentCommand, result: dict[str, Any]) -> None:
    cmd.ok = bool(result.get("ok"))
    cmd.output = str(result.get("output", ""))[:60000]
    cmd.status = "done" if cmd.ok else "failed"
    cmd.finished_at = datetime.now(timezone.utc)
    await db.commit()
    if cmd.kind in BACKUP_KINDS:
        from app.services import backups

        await backups.handle_command_result(db, cmd, result)


async def command_payload(db: AsyncSession, cmd: AgentCommand) -> dict[str, Any]:
    """Wire form of a command. Backup secrets live in settings and are merged in here only."""
    args = dict(cmd.args or {})
    if cmd.kind in BACKUP_KINDS and args.get("plan_id"):
        from app.services import backups

        plan = await db.get(BackupPlan, int(args["plan_id"]))
        dest = await backups.get_destination(db)
        if plan is not None:
            args.update(backups.secret_payload(dest, plan))
    return {"id": cmd.id, "kind": cmd.kind, "args": args}


# ---------- HTTP monitors ----------


async def _check_one(
    client: httpx.AsyncClient, m: Monitor
) -> tuple[bool, int | None, int | None, str | None]:
    start = asyncio.get_event_loop().time()
    try:
        resp = await client.request(m.method, m.url, timeout=m.timeout_s, follow_redirects=True)
        latency = int((asyncio.get_event_loop().time() - start) * 1000)
        ok = resp.status_code == m.expect_status
        error = None
        if ok and m.expect_text and m.expect_text not in resp.text[:200000]:
            ok, error = False, f"нет текста «{m.expect_text}»"
        elif not ok:
            error = f"HTTP {resp.status_code}"
        return ok, resp.status_code, latency, error
    except httpx.HTTPError as exc:
        latency = int((asyncio.get_event_loop().time() - start) * 1000)
        return False, None, latency, type(exc).__name__


async def run_http_checks(db: AsyncSession, only: Monitor | None = None) -> None:
    now = datetime.now(timezone.utc)
    if only is not None:
        monitors = [only]
    else:
        monitors = [
            m
            for m in (await db.scalars(select(Monitor).where(Monitor.enabled.is_(True)))).all()
            if m.last_checked_at is None
            or (now - aware(m.last_checked_at)).total_seconds() >= m.interval_s - 5
        ]
    if not monitors:
        return
    async with httpx.AsyncClient(headers={"User-Agent": "personal-dash-monitor/1"}) as client:
        results = await asyncio.gather(*(_check_one(client, m) for m in monitors))
    for m, (ok, status, latency, error) in zip(monitors, results, strict=True):
        m.last_ok, m.last_status, m.last_latency_ms, m.last_error, m.last_checked_at = (
            ok,
            status,
            latency,
            error,
            now,
        )
        m.consecutive_failures = 0 if ok else m.consecutive_failures + 1
        db.add(CheckSample(monitor_id=m.id, ts=now, ok=ok, status=status, latency_ms=latency))
        if ok:
            await alerts.resolve_incident(
                db, m.project_id, "http_down", str(m.id), f"{m.name} снова отвечает"
            )
        elif m.consecutive_failures >= 2:
            await alerts.open_incident(
                db,
                m.project_id,
                "http_down",
                str(m.id),
                f"{m.name} не отвечает: {error}",
                "critical",
                monitor_id=m.id,
            )
    await db.commit()


async def uptime_stats(db: AsyncSession, monitor_id: int, hours: int = 24) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        (
            await db.scalars(
                select(CheckSample)
                .where(CheckSample.monitor_id == monitor_id, CheckSample.ts >= since)
                .order_by(CheckSample.ts)
            )
        ).all()
    )
    if not rows:
        return {"uptime_pct": None, "samples": []}
    ok = sum(1 for r in rows if r.ok)
    step = max(1, len(rows) // 120)
    return {
        "uptime_pct": round(ok / len(rows) * 100, 2),
        "samples": [{"ts": r.ts.isoformat(), "ok": r.ok, "latency_ms": r.latency_ms} for r in rows[::step]],
    }


async def metric_history(db: AsyncSession, server_id: int, hours: int = 24) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        (
            await db.scalars(
                select(MetricSample)
                .where(MetricSample.server_id == server_id, MetricSample.ts >= since)
                .order_by(MetricSample.ts)
            )
        ).all()
    )
    step = max(1, len(rows) // 150)
    return [
        {"ts": r.ts.isoformat(), "load1": r.load1, "mem_pct": r.mem_pct, "disk_pct": r.disk_pct}
        for r in rows[::step]
    ]


async def cleanup_old_samples(db: AsyncSession) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    await db.execute(delete(MetricSample).where(MetricSample.ts < cutoff))
    await db.execute(delete(CheckSample).where(CheckSample.ts < cutoff))
    await db.execute(delete(AgentCommand).where(AgentCommand.created_at < cutoff))
    await db.commit()


def project_health(incidents: list) -> str:
    """ok | warning | critical based on open incidents."""
    if any(i.severity == "critical" for i in incidents):
        return "critical"
    if incidents:
        return "warning"
    return "ok"
