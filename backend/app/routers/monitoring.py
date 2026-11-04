"""Owner-facing monitoring API: servers, commands, HTTP monitors, incidents."""

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.models import AgentCommand, Incident, Monitor, Project, Server
from app.services import alerts, monitoring

router = APIRouter(prefix="/api", tags=["monitoring"], dependencies=[Depends(require_auth)])


class ServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    disk_alert_pct: int = Field(default=85, ge=50, le=99)
    silence_alert_min: int = Field(default=5, ge=1, le=120)


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    hostname: str | None
    os: str | None
    agent_version: str | None
    last_seen_at: datetime | None
    last_report: dict[str, Any] | None
    disk_alert_pct: int
    silence_alert_min: int
    created_at: datetime
    online: bool = False


class ServerCreated(ServerOut):
    token: str
    install_command: str


class CommandIn(BaseModel):
    kind: Literal["ping", "restart", "stop", "start", "logs", "script", "cron", "config"]
    args: dict[str, Any] = Field(default_factory=dict)


class CommandOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    kind: str
    args: dict[str, Any]
    status: str
    ok: bool | None
    output: str | None
    created_at: datetime
    finished_at: datetime | None


class MonitorIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=4)
    method: Literal["GET", "HEAD", "POST"] = "GET"
    expect_status: int = Field(default=200, ge=100, le=599)
    expect_text: str | None = None
    timeout_s: int = Field(default=10, ge=1, le=60)
    interval_s: int = Field(default=60, ge=30, le=3600)
    enabled: bool = True


class MonitorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    name: str
    url: str
    method: str
    expect_status: int
    expect_text: str | None
    timeout_s: int
    interval_s: int
    enabled: bool
    last_ok: bool | None
    last_status: int | None
    last_latency_ms: int | None
    last_error: str | None
    last_checked_at: datetime | None
    consecutive_failures: int


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    server_id: int | None
    monitor_id: int | None
    kind: str
    key: str
    severity: str
    message: str
    started_at: datetime
    resolved_at: datetime | None


def _server_out(s: Server) -> ServerOut:
    out = ServerOut.model_validate(s)
    if s.last_seen_at:
        out.online = (
            datetime.now(s.last_seen_at.tzinfo) - s.last_seen_at
        ).total_seconds() < s.silence_alert_min * 60
    return out


async def _project(db: AsyncSession, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    return project


async def _server_of(db: AsyncSession, project_id: int, server_id: int) -> Server:
    server = await db.get(Server, server_id)
    if not server or server.project_id != project_id:
        raise HTTPException(404, "Сервер не найден")
    return server


# ---------- servers ----------


@router.get("/projects/{project_id}/servers", response_model=list[ServerOut])
async def list_servers(project_id: int, db: AsyncSession = Depends(get_db)):
    await _project(db, project_id)
    rows = (await db.scalars(select(Server).where(Server.project_id == project_id).order_by(Server.id))).all()
    return [_server_out(s) for s in rows]


@router.post("/projects/{project_id}/servers", response_model=ServerCreated, status_code=201)
async def create_server(project_id: int, body: ServerIn, db: AsyncSession = Depends(get_db)):
    """The plain token is returned once; only its hash is stored."""
    await _project(db, project_id)
    token = monitoring.new_token()
    server = Server(project_id=project_id, token_hash=monitoring.hash_token(token), **body.model_dump())
    db.add(server)
    await db.commit()
    await db.refresh(server)
    url = get_settings().public_url
    out = ServerCreated(
        **_server_out(server).model_dump(),
        token=token,
        install_command=f'curl -fsSL "{url}/api/agent/install?token={token}" | sudo bash',
    )
    return out


@router.put("/projects/{project_id}/servers/{server_id}", response_model=ServerOut)
async def update_server(project_id: int, server_id: int, body: ServerIn, db: AsyncSession = Depends(get_db)):
    server = await _server_of(db, project_id, server_id)
    for k, v in body.model_dump().items():
        setattr(server, k, v)
    await db.commit()
    await db.refresh(server)
    return _server_out(server)


@router.post("/projects/{project_id}/servers/{server_id}/rotate-token", response_model=ServerCreated)
async def rotate_token(project_id: int, server_id: int, db: AsyncSession = Depends(get_db)):
    server = await _server_of(db, project_id, server_id)
    token = monitoring.new_token()
    server.token_hash = monitoring.hash_token(token)
    await db.commit()
    await db.refresh(server)
    url = get_settings().public_url
    return ServerCreated(
        **_server_out(server).model_dump(),
        token=token,
        install_command=f'curl -fsSL "{url}/api/agent/install?token={token}" | sudo bash',
    )


@router.delete("/projects/{project_id}/servers/{server_id}", status_code=204)
async def delete_server(project_id: int, server_id: int, db: AsyncSession = Depends(get_db)):
    server = await _server_of(db, project_id, server_id)
    await db.delete(server)
    await db.commit()


@router.get("/projects/{project_id}/servers/{server_id}/metrics")
async def server_metrics(
    project_id: int, server_id: int, hours: int = 24, db: AsyncSession = Depends(get_db)
):
    await _server_of(db, project_id, server_id)
    return {"samples": await monitoring.metric_history(db, server_id, hours)}


# ---------- commands ----------


@router.post(
    "/projects/{project_id}/servers/{server_id}/commands", response_model=CommandOut, status_code=201
)
async def queue_command(project_id: int, server_id: int, body: CommandIn, db: AsyncSession = Depends(get_db)):
    await _server_of(db, project_id, server_id)
    cmd = AgentCommand(server_id=server_id, kind=body.kind, args=body.args)
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)
    return cmd


@router.get("/projects/{project_id}/servers/{server_id}/commands", response_model=list[CommandOut])
async def list_commands(project_id: int, server_id: int, limit: int = 20, db: AsyncSession = Depends(get_db)):
    await _server_of(db, project_id, server_id)
    stmt = (
        select(AgentCommand)
        .where(AgentCommand.server_id == server_id)
        .order_by(AgentCommand.id.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


@router.get("/projects/{project_id}/servers/{server_id}/commands/{command_id}", response_model=CommandOut)
async def get_command(project_id: int, server_id: int, command_id: int, db: AsyncSession = Depends(get_db)):
    await _server_of(db, project_id, server_id)
    cmd = await db.get(AgentCommand, command_id)
    if not cmd or cmd.server_id != server_id:
        raise HTTPException(404, "Команда не найдена")
    return cmd


# ---------- monitors ----------


@router.get("/projects/{project_id}/monitors", response_model=list[MonitorOut])
async def list_monitors(project_id: int, db: AsyncSession = Depends(get_db)):
    await _project(db, project_id)
    return list(
        (await db.scalars(select(Monitor).where(Monitor.project_id == project_id).order_by(Monitor.id))).all()
    )


@router.post("/projects/{project_id}/monitors", response_model=MonitorOut, status_code=201)
async def create_monitor(project_id: int, body: MonitorIn, db: AsyncSession = Depends(get_db)):
    await _project(db, project_id)
    m = Monitor(project_id=project_id, **body.model_dump())
    db.add(m)
    await db.commit()
    await db.refresh(m)
    try:
        await monitoring.run_http_checks(db, only=m)
    except Exception:  # first check is best-effort
        pass
    await db.refresh(m)
    return m


@router.put("/projects/{project_id}/monitors/{monitor_id}", response_model=MonitorOut)
async def update_monitor(
    project_id: int, monitor_id: int, body: MonitorIn, db: AsyncSession = Depends(get_db)
):
    m = await db.get(Monitor, monitor_id)
    if not m or m.project_id != project_id:
        raise HTTPException(404, "Монитор не найден")
    for k, v in body.model_dump().items():
        setattr(m, k, v)
    await db.commit()
    await db.refresh(m)
    return m


@router.delete("/projects/{project_id}/monitors/{monitor_id}", status_code=204)
async def delete_monitor(project_id: int, monitor_id: int, db: AsyncSession = Depends(get_db)):
    m = await db.get(Monitor, monitor_id)
    if not m or m.project_id != project_id:
        raise HTTPException(404, "Монитор не найден")
    await db.delete(m)
    await db.commit()


@router.post("/projects/{project_id}/monitors/{monitor_id}/check", response_model=MonitorOut)
async def check_now(project_id: int, monitor_id: int, db: AsyncSession = Depends(get_db)):
    m = await db.get(Monitor, monitor_id)
    if not m or m.project_id != project_id:
        raise HTTPException(404, "Монитор не найден")
    await monitoring.run_http_checks(db, only=m)
    await db.refresh(m)
    return m


@router.get("/projects/{project_id}/monitors/{monitor_id}/uptime")
async def monitor_uptime(
    project_id: int, monitor_id: int, hours: int = 24, db: AsyncSession = Depends(get_db)
):
    m = await db.get(Monitor, monitor_id)
    if not m or m.project_id != project_id:
        raise HTTPException(404, "Монитор не найден")
    return await monitoring.uptime_stats(db, monitor_id, hours)


# ---------- incidents ----------


@router.get("/projects/{project_id}/incidents", response_model=list[IncidentOut])
async def project_incidents(project_id: int, limit: int = 50, db: AsyncSession = Depends(get_db)):
    await _project(db, project_id)
    stmt = (
        select(Incident)
        .where(Incident.project_id == project_id)
        .order_by(Incident.started_at.desc())
        .limit(limit)
    )
    return list((await db.scalars(stmt)).all())


@router.get("/incidents", response_model=list[IncidentOut])
async def all_open_incidents(db: AsyncSession = Depends(get_db)):
    return await alerts.open_incidents(db)


@router.post("/projects/{project_id}/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_manually(project_id: int, incident_id: int, db: AsyncSession = Depends(get_db)):
    inc = await db.get(Incident, incident_id)
    if not inc or inc.project_id != project_id:
        raise HTTPException(404, "Инцидент не найден")
    if inc.resolved_at is None:
        inc.resolved_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(inc)
    return inc
