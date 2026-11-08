"""Backup plans: destination settings, scheduling, agent payloads, result handling, alerts."""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AgentCommand, BackupPlan, BackupRun, Project, Server, Setting
from app.services import alerts
from app.utils import aware

log = logging.getLogger(__name__)
DEST_KEY = "backup.destination"
SECRET_FIELDS = ("rclone_conf", "restic_password", "env")
DEST_TYPES = ("rclone", "sftp", "s3", "rest", "local")


# ---------- destination ----------


async def get_destination(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(Setting, DEST_KEY)
    return dict(row.value) if row else {}


async def save_destination(db: AsyncSession, data: dict[str, Any]) -> dict[str, Any]:
    current = await get_destination(db)
    merged = {**current}
    for key, value in data.items():
        if key in SECRET_FIELDS and value in (None, "", "••••••"):
            continue  # keep stored secret when the UI sends the mask back
        merged[key] = value
    if not merged.get("restic_password"):
        merged["restic_password"] = secrets.token_urlsafe(32)
    merged.setdefault("type", "rclone")
    merged.setdefault("base_path", "backups")
    merged.setdefault("remote", "onedrive")
    row = await db.get(Setting, DEST_KEY)
    if row:
        row.value = merged
    else:
        db.add(Setting(key=DEST_KEY, value=merged))
    await db.commit()
    return merged


def masked_destination(dest: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in dest.items() if k not in SECRET_FIELDS}
    out["has_rclone_conf"] = bool(dest.get("rclone_conf"))
    out["has_password"] = bool(dest.get("restic_password"))
    out["env_keys"] = sorted((dest.get("env") or {}).keys())
    out["configured"] = destination_ready(dest)
    return out


def destination_ready(dest: dict[str, Any]) -> bool:
    t = dest.get("type")
    if t == "rclone":
        return bool(dest.get("rclone_conf") and dest.get("remote"))
    if t in ("sftp", "rest", "local"):
        return bool(dest.get("url"))
    if t == "s3":
        return bool(dest.get("url") and (dest.get("env") or {}).get("AWS_ACCESS_KEY_ID"))
    return False


def repo_url(dest: dict[str, Any], plan: BackupPlan) -> str:
    base = (dest.get("base_path") or "backups").strip("/")
    sub = (plan.repo_subpath or f"project-{plan.project_id}").strip("/")
    t = dest.get("type")
    if t == "rclone":
        return f"rclone:{dest.get('remote', 'onedrive')}:{base}/{sub}"
    url = (dest.get("url") or "").rstrip("/")
    if t == "sftp":  # sftp:user@host:/path
        return f"{url}/{base}/{sub}"
    if t == "rest":
        return f"{url}/{base}/{sub}"
    if t == "s3":
        return f"{url}/{base}/{sub}"
    return f"{url}/{base}/{sub}"


def secret_payload(dest: dict[str, Any], plan: BackupPlan) -> dict[str, Any]:
    """Secrets are NOT stored in agent_commands; they are merged in when the command is handed out."""
    payload: dict[str, Any] = {"repo": repo_url(dest, plan), "password": dest.get("restic_password", "")}
    if dest.get("type") == "rclone":
        payload["rclone_conf"] = dest.get("rclone_conf", "")
    if dest.get("env"):
        payload["env"] = dest["env"]
    return payload


# ---------- scheduling ----------


def compute_next_run(schedule: str, after: datetime | None = None) -> datetime | None:
    try:
        trigger = CronTrigger.from_crontab(schedule, timezone=get_settings().tz)
    except ValueError:
        return None
    base = after or datetime.now(timezone.utc)
    return trigger.get_next_fire_time(None, base)


def validate_schedule(schedule: str) -> bool:
    try:
        CronTrigger.from_crontab(schedule)
        return True
    except ValueError:
        return False


async def queue_run(
    db: AsyncSession,
    plan: BackupPlan,
    kind: str = "backup",
    trigger: str = "manual",
    extra: dict[str, Any] | None = None,
) -> BackupRun:
    """Create an AgentCommand for the plan's server; the agent executes it in the background."""
    args: dict[str, Any] = {"plan_id": plan.id, "plan_name": plan.name}
    if kind == "backup":
        args.update(
            {
                "items": plan.items,
                "retention": plan.retention or {"daily": 7, "weekly": 4, "monthly": 6},
                "prune": datetime.now(timezone.utc).weekday() == 6 or bool((extra or {}).get("prune")),
            }
        )
    command_kind = {
        "backup": "backup",
        "snapshots": "backup_snapshots",
        "restore": "restore",
        "check": "backup_check",
    }[kind]
    if extra:
        args.update({k: v for k, v in extra.items() if k != "prune"})
    cmd = AgentCommand(server_id=plan.server_id, kind=command_kind, args=args)
    db.add(cmd)
    await db.flush()
    run = BackupRun(plan_id=plan.id, command_id=cmd.id, kind=kind, trigger=trigger, status="running")
    db.add(run)
    if kind == "backup":
        plan.last_run_at = datetime.now(timezone.utc)
        plan.last_status = "running"
    await db.commit()
    await db.refresh(run)
    return run


CHECK_EVERY = timedelta(days=7)


async def _check_due(db: AsyncSession, plan: BackupPlan, now: datetime) -> bool:
    """A weekly `restic check` for plans that have at least one successful backup and are idle."""
    if plan.last_status != "ok" or plan.last_ok_at is None:
        return False
    running = await db.scalar(
        select(func.count())
        .select_from(BackupRun)
        .where(BackupRun.plan_id == plan.id, BackupRun.status == "running")
    )
    if running:
        return False
    last = await db.scalar(
        select(func.max(BackupRun.started_at)).where(BackupRun.plan_id == plan.id, BackupRun.kind == "check")
    )
    return last is None or now - aware(last) >= CHECK_EVERY


async def tick(db: AsyncSession) -> None:
    """Every minute: start due plans, flag overdue ones, run the weekly check, time out stuck runs."""
    now = datetime.now(timezone.utc)
    dest = await get_destination(db)
    plans = list((await db.scalars(select(BackupPlan).where(BackupPlan.enabled.is_(True)))).all())
    for plan in plans:
        if destination_ready(dest) and await _check_due(db, plan, now):
            await queue_run(db, plan, "check", trigger="schedule")
            continue  # one job per plan per tick; the backup itself gets the next minute
        nxt = aware(plan.next_run_at)
        if nxt is None:
            plan.next_run_at = compute_next_run(plan.schedule, now)
            continue
        if nxt <= now:
            plan.next_run_at = compute_next_run(plan.schedule, now)
            if not destination_ready(dest):
                await alerts.open_incident(
                    db,
                    plan.project_id,
                    "backup_failed",
                    str(plan.id),
                    f"бэкап «{plan.name}»: хранилище не настроено",
                    "warning",
                )
                continue
            if (
                plan.last_status == "running"
                and plan.last_run_at
                and now - aware(plan.last_run_at) < timedelta(hours=6)
            ):
                log.info("backup plan %s still running, skipping this tick", plan.id)
                continue
            await queue_run(db, plan, "backup", trigger="schedule")
        # overdue?
        last_ok = aware(plan.last_ok_at)
        age_limit = timedelta(hours=plan.max_age_hours or 36)
        if (last_ok is None and now - aware(plan.created_at) > age_limit) or (
            last_ok is not None and now - last_ok > age_limit
        ):
            await alerts.open_incident(
                db,
                plan.project_id,
                "backup_overdue",
                str(plan.id),
                f"бэкап «{plan.name}» не выполнялся успешно дольше {plan.max_age_hours} ч",
                "warning",
            )
    # stuck runs
    stale_cut = now - timedelta(hours=6)
    runs = list(
        (
            await db.scalars(
                select(BackupRun).where(BackupRun.status == "running", BackupRun.started_at < stale_cut)
            )
        ).all()
    )
    for run in runs:
        run.status = "failed"
        run.finished_at = now
        run.output = (run.output or "") + "\nTIMEOUT: no result from agent within 6 h"
        plan = await db.get(BackupPlan, run.plan_id)
        if plan and plan.last_status == "running":
            plan.last_status = "failed"
    await db.commit()


# ---------- results ----------


async def handle_command_result(db: AsyncSession, cmd: AgentCommand, result: dict[str, Any]) -> None:
    """Called from monitoring.finish_command for backup-related commands."""
    run = await db.scalar(select(BackupRun).where(BackupRun.command_id == cmd.id))
    if run is None:
        return
    plan = await db.get(BackupPlan, run.plan_id)
    now = datetime.now(timezone.utc)
    ok = bool(result.get("ok"))
    run.status = "ok" if ok else "failed"
    run.finished_at = now
    run.duration_s = int(result.get("duration") or (now - aware(run.started_at)).total_seconds())
    run.output = str(result.get("output", ""))[:20000]
    run.results = result.get("results") or result.get("snapshots")
    if run.results and isinstance(run.results, list) and run.kind == "backup":
        run.added_bytes = sum(int(r.get("added") or 0) for r in run.results if isinstance(r, dict))
    if plan is None:
        await db.commit()
        return
    if run.kind == "backup":
        plan.last_status = run.status
        if ok:
            plan.last_ok_at = now
            if result.get("repo_size") is not None:
                plan.repo_size = int(result["repo_size"])
            await alerts.resolve_incident(
                db, plan.project_id, "backup_failed", str(plan.id), f"бэкап «{plan.name}» выполнен"
            )
            await alerts.resolve_incident(
                db, plan.project_id, "backup_overdue", str(plan.id), f"бэкап «{plan.name}» выполнен"
            )
            # the UI shows plan.snapshots; refresh it without waiting for a manual request
            await queue_run(db, plan, "snapshots", trigger="auto")
        else:
            errors = "; ".join(result.get("errors") or [])[:200] or run.output[-200:]
            await alerts.open_incident(
                db,
                plan.project_id,
                "backup_failed",
                str(plan.id),
                f"бэкап «{plan.name}» упал: {errors}",
                "critical",
            )
    elif run.kind == "snapshots" and ok:
        plan.snapshots = result.get("snapshots") or []
    elif run.kind == "check":
        if ok:
            await alerts.resolve_incident(
                db, plan.project_id, "backup_check", str(plan.id), f"репозиторий «{plan.name}» цел"
            )
        else:
            await alerts.open_incident(
                db,
                plan.project_id,
                "backup_check",
                str(plan.id),
                f"restic check «{plan.name}» нашёл ошибки",
                "critical",
            )
    await db.commit()


async def plan_summary(db: AsyncSession, project_id: int) -> list[dict[str, Any]]:
    plans = list(
        (
            await db.scalars(
                select(BackupPlan).where(BackupPlan.project_id == project_id).order_by(BackupPlan.id)
            )
        ).all()
    )
    servers = {
        s.id: s for s in (await db.scalars(select(Server).where(Server.project_id == project_id))).all()
    }
    return [{"plan": p, "server": servers.get(p.server_id)} for p in plans]


def recovery_sheet(dest: dict[str, Any], plans: list[tuple[Project, BackupPlan]]) -> str:
    """Plain-text sheet with everything needed to restore without the dashboard."""
    lines = [
        "PERSONAL DASH — BACKUP RECOVERY SHEET",
        f"generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "",
    ]
    lines += ["RESTIC_PASSWORD:", dest.get("restic_password", ""), ""]
    if dest.get("type") == "rclone":
        lines += ["rclone.conf (save as ~/.config/rclone/rclone.conf):", dest.get("rclone_conf", ""), ""]
    if dest.get("env"):
        lines += ["environment:"] + [f"  {k}={v}" for k, v in dest["env"].items()] + [""]
    lines.append("repositories:")
    for project, plan in plans:
        lines.append(f"  {project.name} / {plan.name}: {repo_url(dest, plan)}")
    lines += [
        "",
        "restore example:",
        "  export RESTIC_PASSWORD=...; restic -r <repo> snapshots",
        "  restic -r <repo> restore latest --target ./restored",
    ]
    return "\n".join(lines)
