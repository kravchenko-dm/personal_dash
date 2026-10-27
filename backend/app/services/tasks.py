from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task
from app.schemas import TaskIn, TaskPatch


async def list_tasks(db: AsyncSession, status: str | None = None) -> list[Task]:
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status == status)
    stmt = stmt.order_by(Task.status, Task.priority, Task.due_at.nulls_last(), Task.created_at.desc())
    return list((await db.scalars(stmt)).all())


async def create_task(
    db: AsyncSession, data: TaskIn, source: str = "manual", source_ref: str | None = None
) -> Task:
    task = Task(**data.model_dump(), source=source, source_ref=source_ref)
    if task.status == "done":
        task.completed_at = datetime.now(timezone.utc)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def get_task(db: AsyncSession, task_id: int) -> Task | None:
    return await db.get(Task, task_id)


async def patch_task(db: AsyncSession, task: Task, data: TaskPatch) -> Task:
    changes = data.model_dump(exclude_unset=True, exclude={"clear_due"})
    for key, value in changes.items():
        setattr(task, key, value)
    if data.clear_due:
        task.due_at = None
    if "status" in changes:
        task.completed_at = datetime.now(timezone.utc) if task.status == "done" else None
    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, task: Task) -> None:
    await db.delete(task)
    await db.commit()


async def open_tasks_text(db: AsyncSession, limit: int = 30) -> str:
    """Plain-text list of open tasks for Telegram replies and summaries."""
    tasks = [t for t in await list_tasks(db) if t.status != "done"][:limit]
    if not tasks:
        return "Открытых задач нет."
    marks = {1: "🔴", 2: "🟡", 3: "🟢"}
    lines = []
    for t in tasks:
        due = f" · до {t.due_at.astimezone().strftime('%d.%m %H:%M')}" if t.due_at else ""
        lines.append(f"{marks.get(t.priority, '•')} #{t.id} {t.title}{due}")
    return "\n".join(lines)
