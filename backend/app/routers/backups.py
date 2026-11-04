"""Backup destination settings and per-project backup plans."""

import re
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import BackupPlan, BackupRun, Project, Server
from app.services import backups as svc

router = APIRouter(prefix="/api", tags=["backups"], dependencies=[Depends(require_auth)])

ItemType = Literal["path", "pg_dump", "mysql_dump", "docker_volume", "clickhouse"]


class DestinationIn(BaseModel):
    type: Literal["rclone", "sftp", "s3", "rest", "local"] = "rclone"
    remote: str | None = "onedrive"  # rclone remote name
    base_path: str = "backups"
    url: str | None = None  # sftp:user@host:/path | rest:https://... | s3:https://host/bucket | /mnt/backups
    rclone_conf: str | None = None
    restic_password: str | None = None
    env: dict[str, str] | None = None


class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    server_id: int
    enabled: bool = True
    items: list[dict[str, Any]] = Field(default_factory=list)
    schedule: str = "0 3 * * *"
    retention: dict[str, int] = Field(default_factory=lambda: {"daily": 7, "weekly": 4, "monthly": 6})
    max_age_hours: int = Field(default=36, ge=1, le=24 * 60)
    repo_subpath: str = ""


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    server_id: int
    name: str
    enabled: bool
    items: list[dict[str, Any]]
    schedule: str
    retention: dict[str, int]
    max_age_hours: int
    repo_subpath: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_ok_at: datetime | None
    last_status: str | None
    repo_size: int | None
    snapshots: list[dict[str, Any]] | None
    repo: str = ""
    server_name: str = ""


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    command_id: int | None
    kind: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_s: int | None
    added_bytes: int | None
    results: list[dict[str, Any]] | None
    output: str | None


class RestoreIn(BaseModel):
    snapshot_id: str = Field(min_length=4, max_length=64)
    include: list[str] = Field(default_factory=list)


def _validate_items(items: list[dict[str, Any]]) -> None:
    for it in items:
        t = it.get("type")
        if t == "path" and not it.get("path"):
            raise HTTPException(400, "Элемент «каталог» без пути")
        if (
            t in ("pg_dump", "mysql_dump")
            and it.get("mode", "docker") == "docker"
            and not it.get("container")
        ):
            raise HTTPException(400, f"Элемент «{t}» в режиме docker требует имя контейнера")
        if t == "docker_volume" and not it.get("volume"):
            raise HTTPException(400, "Элемент «том docker» без имени тома")
        if t == "clickhouse":
            if it.get("mode", "docker") == "docker" and not it.get("container"):
                raise HTTPException(400, "Элемент «ClickHouse» в режиме docker требует имя контейнера")
            if not re.fullmatch(r"[A-Za-z0-9_]+", str(it.get("table") or "")) or not re.fullmatch(
                r"[A-Za-z0-9_]+", str(it.get("database") or "default")
            ):
                raise HTTPException(400, "Элемент «ClickHouse»: база и таблица — простые идентификаторы")
        if t not in ("path", "pg_dump", "mysql_dump", "docker_volume", "clickhouse"):
            raise HTTPException(400, f"Неизвестный тип элемента: {t}")


async def _plan_out(db: AsyncSession, plan: BackupPlan, dest: dict | None = None) -> PlanOut:
    dest = dest if dest is not None else await svc.get_destination(db)
    server = await db.get(Server, plan.server_id)
    out = PlanOut.model_validate(plan)
    out.repo = svc.repo_url(dest, plan) if dest else ""
    out.server_name = server.name if server else "?"
    return out


async def _plan_or_404(db: AsyncSession, project_id: int, plan_id: int) -> BackupPlan:
    plan = await db.get(BackupPlan, plan_id)
    if not plan or plan.project_id != project_id:
        raise HTTPException(404, "План не найден")
    return plan


# ---------- destination ----------


@router.get("/backups/destination")
async def get_destination(db: AsyncSession = Depends(get_db)):
    return svc.masked_destination(await svc.get_destination(db))


@router.put("/backups/destination")
async def put_destination(body: DestinationIn, db: AsyncSession = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if (
        body.type == "rclone"
        and body.rclone_conf
        and body.rclone_conf != "••••••"
        and "[" not in body.rclone_conf
    ):
        raise HTTPException(400, "rclone.conf должен содержать секцию вида [onedrive]")
    saved = await svc.save_destination(db, data)
    return svc.masked_destination(saved)


@router.get("/backups/recovery-sheet", response_class=PlainTextResponse)
async def recovery_sheet(db: AsyncSession = Depends(get_db)):
    dest = await svc.get_destination(db)
    if not dest:
        raise HTTPException(400, "Хранилище не настроено")
    rows = (
        await db.execute(select(Project, BackupPlan).join(BackupPlan, BackupPlan.project_id == Project.id))
    ).all()
    return svc.recovery_sheet(dest, [(p, b) for p, b in rows])


@router.get("/backups/overview")
async def overview(db: AsyncSession = Depends(get_db)):
    dest = await svc.get_destination(db)
    plans = list((await db.scalars(select(BackupPlan).order_by(BackupPlan.project_id, BackupPlan.id))).all())
    return {
        "destination": svc.masked_destination(dest),
        "plans": [await _plan_out(db, p, dest) for p in plans],
    }


# ---------- plans ----------


@router.get("/projects/{project_id}/backups", response_model=list[PlanOut])
async def list_plans(project_id: int, db: AsyncSession = Depends(get_db)):
    if not await db.get(Project, project_id):
        raise HTTPException(404, "Проект не найден")
    dest = await svc.get_destination(db)
    plans = (
        await db.scalars(
            select(BackupPlan).where(BackupPlan.project_id == project_id).order_by(BackupPlan.id)
        )
    ).all()
    return [await _plan_out(db, p, dest) for p in plans]


@router.post("/projects/{project_id}/backups", response_model=PlanOut, status_code=201)
async def create_plan(project_id: int, body: PlanIn, db: AsyncSession = Depends(get_db)):
    if not await db.get(Project, project_id):
        raise HTTPException(404, "Проект не найден")
    server = await db.get(Server, body.server_id)
    if not server or server.project_id != project_id:
        raise HTTPException(400, "Сервер не принадлежит проекту")
    if not svc.validate_schedule(body.schedule):
        raise HTTPException(400, "Неверное cron-расписание")
    _validate_items(body.items)
    plan = BackupPlan(project_id=project_id, **body.model_dump())
    plan.next_run_at = svc.compute_next_run(plan.schedule)
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return await _plan_out(db, plan)


@router.put("/projects/{project_id}/backups/{plan_id}", response_model=PlanOut)
async def update_plan(project_id: int, plan_id: int, body: PlanIn, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, project_id, plan_id)
    server = await db.get(Server, body.server_id)
    if not server or server.project_id != project_id:
        raise HTTPException(400, "Сервер не принадлежит проекту")
    if not svc.validate_schedule(body.schedule):
        raise HTTPException(400, "Неверное cron-расписание")
    _validate_items(body.items)
    reschedule = body.schedule != plan.schedule
    for k, v in body.model_dump().items():
        setattr(plan, k, v)
    if reschedule or plan.next_run_at is None:
        plan.next_run_at = svc.compute_next_run(plan.schedule)
    await db.commit()
    await db.refresh(plan)
    return await _plan_out(db, plan)


@router.delete("/projects/{project_id}/backups/{plan_id}", status_code=204)
async def delete_plan(project_id: int, plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, project_id, plan_id)
    await db.delete(plan)
    await db.commit()


@router.post("/projects/{project_id}/backups/{plan_id}/run", response_model=RunOut)
async def run_now(project_id: int, plan_id: int, prune: bool = False, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, project_id, plan_id)
    if not svc.destination_ready(await svc.get_destination(db)):
        raise HTTPException(400, "Сначала настройте хранилище бэкапов (Ещё → Бэкапы)")
    if not plan.items:
        raise HTTPException(400, "В плане нет элементов")
    return await svc.queue_run(db, plan, "backup", trigger="manual", extra={"prune": prune})


@router.post("/projects/{project_id}/backups/{plan_id}/snapshots", response_model=RunOut)
async def refresh_snapshots(project_id: int, plan_id: int, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, project_id, plan_id)
    if not svc.destination_ready(await svc.get_destination(db)):
        raise HTTPException(400, "Хранилище не настроено")
    return await svc.queue_run(db, plan, "snapshots", trigger="manual")


@router.post("/projects/{project_id}/backups/{plan_id}/restore", response_model=RunOut)
async def restore(project_id: int, plan_id: int, body: RestoreIn, db: AsyncSession = Depends(get_db)):
    """Restores into /var/backups/dash-restore/<plan>/<snapshot> on the server, never in place."""
    plan = await _plan_or_404(db, project_id, plan_id)
    if not svc.destination_ready(await svc.get_destination(db)):
        raise HTTPException(400, "Хранилище не настроено")
    return await svc.queue_run(
        db,
        plan,
        "restore",
        trigger="manual",
        extra={"snapshot_id": body.snapshot_id, "include": body.include},
    )


@router.post("/projects/{project_id}/backups/{plan_id}/check", response_model=RunOut)
async def check(project_id: int, plan_id: int, read_data: bool = False, db: AsyncSession = Depends(get_db)):
    plan = await _plan_or_404(db, project_id, plan_id)
    if not svc.destination_ready(await svc.get_destination(db)):
        raise HTTPException(400, "Хранилище не настроено")
    extra = {"read_data_subset": "5%"} if read_data else {}
    return await svc.queue_run(db, plan, "check", trigger="manual", extra=extra)


@router.get("/projects/{project_id}/backups/{plan_id}/runs", response_model=list[RunOut])
async def runs(project_id: int, plan_id: int, limit: int = 20, db: AsyncSession = Depends(get_db)):
    await _plan_or_404(db, project_id, plan_id)
    stmt = select(BackupRun).where(BackupRun.plan_id == plan_id).order_by(BackupRun.id.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


@router.get("/projects/{project_id}/backups/{plan_id}/runs/{run_id}", response_model=RunOut)
async def run_detail(project_id: int, plan_id: int, run_id: int, db: AsyncSession = Depends(get_db)):
    await _plan_or_404(db, project_id, plan_id)
    run = await db.get(BackupRun, run_id)
    if not run or run.plan_id != plan_id:
        raise HTTPException(404, "Запуск не найден")
    return run
