import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_auth
from app.services import yougile as svc

router = APIRouter(prefix="/api/yougile", tags=["yougile"], dependencies=[Depends(require_auth)])


class TaskPatch(BaseModel):
    completed: bool | None = None
    column_id: str | None = None
    title: str | None = None


class TaskCreate(BaseModel):
    title: str
    column_id: str
    description: str = ""


def _wrap(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(502, f"YouGile {exc.response.status_code}: {exc.response.text[:200]}")
    return HTTPException(502, f"YouGile: {exc}")


@router.get("/overview")
async def overview():
    try:
        return await svc.overview()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise _wrap(exc) from exc


@router.post("/refresh")
async def refresh():
    svc.invalidate()
    return {"ok": True}


@router.patch("/tasks/{task_id}")
async def patch_task(task_id: str, body: TaskPatch):
    patch: dict = {}
    if body.completed is not None:
        patch["completed"] = body.completed
    if body.column_id:
        patch["columnId"] = body.column_id
    if body.title:
        patch["title"] = body.title
    try:
        return await svc.update_task(task_id, patch)
    except Exception as exc:
        raise _wrap(exc) from exc


@router.post("/tasks", status_code=201)
async def create_task(body: TaskCreate):
    try:
        return await svc.create_task(body.title, body.column_id, body.description)
    except Exception as exc:
        raise _wrap(exc) from exc
