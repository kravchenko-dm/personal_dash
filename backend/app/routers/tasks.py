from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.schemas import TaskIn, TaskOut, TaskPatch
from app.services import tasks as svc

router = APIRouter(prefix="/api/tasks", tags=["tasks"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[TaskOut])
async def list_tasks(status: str | None = None, db: AsyncSession = Depends(get_db)):
    return await svc.list_tasks(db, status)


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(body: TaskIn, db: AsyncSession = Depends(get_db)):
    return await svc.create_task(db, body)


@router.patch("/{task_id}", response_model=TaskOut)
async def patch_task(task_id: int, body: TaskPatch, db: AsyncSession = Depends(get_db)):
    task = await svc.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return await svc.patch_task(db, task, body)


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await svc.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await svc.delete_task(db, task)
