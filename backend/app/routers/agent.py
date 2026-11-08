"""Endpoints used by dash-agent on the servers (token auth, not JWT) + installer download."""

import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import AgentCommand, Server
from app.services import monitoring, tool_mirror

router = APIRouter(prefix="/api/agent", tags=["agent"])
_HERE = Path(__file__).resolve()
AGENT_DIR = next(
    (d for d in (_HERE.parents[3] / "agent", _HERE.parents[2] / "agent") if d.exists()),
    _HERE.parents[3] / "agent",
)  # repo checkout: <root>/agent; docker image: /app/agent


@lru_cache
def bundled_agent_version() -> str | None:
    path = AGENT_DIR / "dash_agent.py"
    if not path.exists():
        return None
    m = re.search(r'^AGENT_VERSION = "([^"]+)"', path.read_text(), re.M)
    return m.group(1) if m else None


async def _server(db: AsyncSession, token: str | None) -> Server:
    server = await monitoring.server_by_token(db, token or "")
    if not server:
        raise HTTPException(401, "bad agent token")
    return server


@router.post("/report")
async def report(
    request: Request, x_agent_token: str | None = Header(default=None), db: AsyncSession = Depends(get_db)
):
    server = await _server(db, x_agent_token)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(400, "report must be a JSON object")
    commands = await monitoring.ingest_report(db, server, payload)
    return {
        "ok": True,
        "agent_version": bundled_agent_version(),
        "commands": [await monitoring.command_payload(db, c) for c in commands],
    }


@router.post("/commands/{command_id}/result")
async def command_result(
    command_id: int,
    request: Request,
    x_agent_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    server = await _server(db, x_agent_token)
    cmd = await db.get(AgentCommand, command_id)
    if not cmd or cmd.server_id != server.id:
        raise HTTPException(404, "unknown command")
    await monitoring.finish_command(db, cmd, await request.json())
    return {"ok": True}


@router.get("/script", response_class=PlainTextResponse)
async def script():
    path = AGENT_DIR / "dash_agent.py"
    if not path.exists():
        raise HTTPException(404, "agent script not bundled")
    return path.read_text()


@router.get("/install", response_class=PlainTextResponse)
async def install(token: str = Query(min_length=10), db: AsyncSession = Depends(get_db)):
    await _server(db, token)  # only a valid token gets an installer
    tmpl = (AGENT_DIR / "install.sh.tmpl").read_text()
    return tmpl.replace("{{URL}}", get_settings().public_url).replace("{{TOKEN}}", token)


@router.get("/tools/{name}")
async def tool(
    name: str,
    arch: str = Query(default="amd64", pattern="^[a-z0-9]+$"),
    x_agent_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    """restic / rclone binary for agents whose server cannot reach GitHub directly."""
    await _server(db, x_agent_token)
    try:
        path = await tool_mirror.ensure_tool(name, arch)
    except KeyError:
        raise HTTPException(404, "unknown tool or arch") from None
    except Exception as exc:
        raise HTTPException(502, f"не удалось скачать {name}: {exc}") from exc
    return FileResponse(path, media_type="application/octet-stream", filename=name)
