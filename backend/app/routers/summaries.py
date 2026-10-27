from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import Summary
from app.schemas import SummaryOut, TaskIn, TaskOut
from app.services import summaries as svc
from app.services.tasks import create_task

router = APIRouter(prefix="/api/summaries", tags=["summaries"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[SummaryOut])
async def list_summaries(limit: int = 20, db: AsyncSession = Depends(get_db)):
    stmt = select(Summary).order_by(Summary.created_at.desc()).limit(limit)
    return list((await db.scalars(stmt)).all())


@router.post("", response_model=SummaryOut | None)
async def make_summary(chat_id: int | None = None, hours: int = 24, db: AsyncSession = Depends(get_db)):
    try:
        return await svc.build_summary(db, chat_id=chat_id, hours=hours)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{summary_id}/accept/{index}", response_model=TaskOut)
async def accept_task(summary_id: int, index: int, db: AsyncSession = Depends(get_db)):
    summary = await db.get(Summary, summary_id)
    if not summary or index >= len(summary.proposed_tasks):
        raise HTTPException(404, "Proposal not found")
    p = summary.proposed_tasks[index]
    return await create_task(
        db,
        TaskIn(title=p["title"], notes=p.get("reason"), priority=int(p.get("priority", 2))),
        source="ai",
        source_ref=f"summary:{summary.id}",
    )
