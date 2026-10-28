from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import Feature, Summary
from app.routers.projects import FeatureOut
from app.schemas import SummaryOut, TaskIn, TaskOut
from app.services import projects as projects_svc
from app.services import summaries as svc
from app.services.llm import LLMError
from app.services.tasks import create_task

router = APIRouter(prefix="/api/summaries", tags=["summaries"], dependencies=[Depends(require_auth)])


@router.get("", response_model=list[SummaryOut])
async def list_summaries(limit: int = 20, project_id: int | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(Summary).order_by(Summary.created_at.desc()).limit(limit)
    if project_id is not None:
        stmt = stmt.where(Summary.project_id == project_id)
    return list((await db.scalars(stmt)).all())


@router.post("", response_model=SummaryOut | None)
async def make_summary(
    chat_id: int | None = None,
    hours: int = 24,
    project_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await svc.build_summary(db, chat_id=chat_id, hours=hours, project_id=project_id)
    except (LLMError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/{summary_id}/accept-feature/{index}", response_model=FeatureOut)
async def accept_feature(
    summary_id: int, index: int, project_id: int | None = None, db: AsyncSession = Depends(get_db)
):
    """Turn a proposal into a card on the project's feature board (status: idea)."""
    summary = await db.get(Summary, summary_id)
    if not summary or index >= len(summary.proposed_tasks):
        raise HTTPException(404, "Proposal not found")
    pid = project_id or summary.project_id
    if pid is None:
        raise HTTPException(400, "Саммари не привязано к проекту, укажите project_id")
    p = summary.proposed_tasks[index]
    feature = Feature(
        project_id=pid,
        title=p["title"],
        description=p.get("reason"),
        priority=int(p.get("priority", 2)),
        status="idea",
        source="ai",
        source_ref=f"summary:{summary.id}",
    )
    feature.position = await projects_svc.next_position(db, pid, "idea")
    db.add(feature)
    await db.commit()
    await db.refresh(feature)
    return feature


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
