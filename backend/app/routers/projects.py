from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_db
from app.models import Feature, Project, ProjectNote, Summary, TelegramChat
from app.schemas import SummaryOut, TelegramChatOut
from app.services import alerts, monitoring
from app.services import projects as svc

router = APIRouter(prefix="/api/projects", tags=["projects"], dependencies=[Depends(require_auth)])

Stage = Literal["idea", "dev", "beta", "prod", "paused"]
FeatureStatus = Literal["idea", "planned", "in_progress", "testing", "done"]


# ---------- schemas ----------


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    stage: Stage = "dev"
    color: str = Field(default="#63a0ff", pattern=r"^#[0-9a-fA-F]{6}$")
    repo_url: str | None = None
    site_url: str | None = None
    yougile_board_id: str | None = None
    archived: bool = False


class StatusNoteIn(BaseModel):
    status_note: str | None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    description: str | None
    stage: Stage
    color: str
    repo_url: str | None
    site_url: str | None
    yougile_board_id: str | None
    status_note: str | None
    status_note_at: datetime | None
    archived: bool
    created_at: datetime
    updated_at: datetime
    feature_counts: dict[str, int] = Field(default_factory=dict)
    chat_count: int = 0
    health: str = "ok"  # ok | warning | critical
    open_incidents: int = 0


class FeatureIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = None
    status: FeatureStatus = "idea"
    priority: int = Field(default=2, ge=1, le=3)
    eta: datetime | None = None
    link_url: str | None = None


class FeaturePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    eta: datetime | None = None
    clear_eta: bool = False
    link_url: str | None = None


class FeatureMove(BaseModel):
    status: FeatureStatus
    index: int | None = None


class FeatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    description: str | None
    status: FeatureStatus
    priority: int
    eta: datetime | None
    position: int
    link_url: str | None
    source: str
    created_at: datetime
    updated_at: datetime
    done_at: datetime | None


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = ""
    pinned: bool = False


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    title: str
    body: str
    pinned: bool
    position: int
    created_at: datetime
    updated_at: datetime


class ProjectDetail(BaseModel):
    project: ProjectOut
    features: list[FeatureOut]
    notes: list[NoteOut]
    chats: list[TelegramChatOut]
    summaries: list[SummaryOut]


# ---------- helpers ----------


async def _project_or_404(db: AsyncSession, project_id: int) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    return project


async def _out(db: AsyncSession, project: Project) -> ProjectOut:
    counts = await svc.feature_counts(db, [project.id])
    chats = await svc.chats_for_project(db, project.id)
    out = ProjectOut.model_validate(project)
    out.feature_counts = counts.get(project.id, {})
    out.chat_count = len(chats)
    incidents = await alerts.open_incidents(db, project.id)
    out.health = monitoring.project_health(incidents)
    out.open_incidents = len(incidents)
    return out


# ---------- projects ----------


@router.get("", response_model=list[ProjectOut])
async def list_projects(archived: bool = False, db: AsyncSession = Depends(get_db)):
    stmt = select(Project).where(Project.archived.is_(archived)).order_by(Project.created_at)
    projects = list((await db.scalars(stmt)).all())
    counts = await svc.feature_counts(db, [p.id for p in projects])
    chat_rows = (
        await db.execute(select(TelegramChat.project_id).where(TelegramChat.project_id.is_not(None)))
    ).all()
    chat_counts: dict[int, int] = {}
    for (pid,) in chat_rows:
        chat_counts[pid] = chat_counts.get(pid, 0) + 1
    incidents = await alerts.open_incidents(db)
    out = []
    for p in projects:
        item = ProjectOut.model_validate(p)
        item.feature_counts = counts.get(p.id, {})
        item.chat_count = chat_counts.get(p.id, 0)
        mine = [i for i in incidents if i.project_id == p.id]
        item.health = monitoring.project_health(mine)
        item.open_incidents = len(mine)
        out.append(item)
    return out


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectIn, db: AsyncSession = Depends(get_db)):
    project = Project(**body.model_dump(), slug=await svc.unique_slug(db, body.name))
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return await _out(db, project)


@router.get("/{project_id}", response_model=ProjectDetail)
async def project_detail(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await _project_or_404(db, project_id)
    summaries = list(
        (
            await db.scalars(
                select(Summary)
                .where(Summary.project_id == project_id)
                .order_by(Summary.created_at.desc())
                .limit(5)
            )
        ).all()
    )
    return ProjectDetail(
        project=await _out(db, project),
        features=[FeatureOut.model_validate(f) for f in await svc.project_features(db, project_id)],
        notes=[NoteOut.model_validate(n) for n in await svc.project_notes(db, project_id)],
        chats=[TelegramChatOut.model_validate(c) for c in await svc.chats_for_project(db, project_id)],
        summaries=[SummaryOut.model_validate(s) for s in summaries],
    )


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(project_id: int, body: ProjectIn, db: AsyncSession = Depends(get_db)):
    project = await _project_or_404(db, project_id)
    if body.name != project.name:
        project.slug = await svc.unique_slug(db, body.name, exclude_id=project.id)
    for key, value in body.model_dump().items():
        setattr(project, key, value)
    await db.commit()
    await db.refresh(project)
    return await _out(db, project)


@router.patch("/{project_id}/status", response_model=ProjectOut)
async def set_status_note(project_id: int, body: StatusNoteIn, db: AsyncSession = Depends(get_db)):
    project = await _project_or_404(db, project_id)
    project.status_note = body.status_note or None
    project.status_note_at = datetime.now(timezone.utc) if body.status_note else None
    await db.commit()
    await db.refresh(project)
    return await _out(db, project)


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: int, db: AsyncSession = Depends(get_db)):
    project = await _project_or_404(db, project_id)
    await db.delete(project)
    await db.commit()


# ---------- features ----------


@router.get("/{project_id}/features", response_model=list[FeatureOut])
async def list_features(project_id: int, db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    return await svc.project_features(db, project_id)


@router.post("/{project_id}/features", response_model=FeatureOut, status_code=201)
async def create_feature(project_id: int, body: FeatureIn, db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    feature = Feature(project_id=project_id, **body.model_dump())
    feature.position = await svc.next_position(db, project_id, body.status)
    if body.status == "done":
        feature.done_at = datetime.now(timezone.utc)
    db.add(feature)
    await db.commit()
    await db.refresh(feature)
    return feature


async def _feature_or_404(db: AsyncSession, project_id: int, feature_id: int) -> Feature:
    feature = await db.get(Feature, feature_id)
    if not feature or feature.project_id != project_id:
        raise HTTPException(404, "Фича не найдена")
    return feature


@router.patch("/{project_id}/features/{feature_id}", response_model=FeatureOut)
async def patch_feature(
    project_id: int, feature_id: int, body: FeaturePatch, db: AsyncSession = Depends(get_db)
):
    feature = await _feature_or_404(db, project_id, feature_id)
    for key, value in body.model_dump(exclude_unset=True, exclude={"clear_eta"}).items():
        setattr(feature, key, value)
    if body.clear_eta:
        feature.eta = None
    await db.commit()
    await db.refresh(feature)
    return feature


@router.post("/{project_id}/features/{feature_id}/move", response_model=FeatureOut)
async def move_feature(
    project_id: int, feature_id: int, body: FeatureMove, db: AsyncSession = Depends(get_db)
):
    feature = await _feature_or_404(db, project_id, feature_id)
    return await svc.move_feature(db, feature, body.status, body.index)


@router.delete("/{project_id}/features/{feature_id}", status_code=204)
async def delete_feature(project_id: int, feature_id: int, db: AsyncSession = Depends(get_db)):
    feature = await _feature_or_404(db, project_id, feature_id)
    await db.delete(feature)
    await db.commit()


# ---------- notes ----------


@router.post("/{project_id}/notes", response_model=NoteOut, status_code=201)
async def create_note(project_id: int, body: NoteIn, db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    note = ProjectNote(project_id=project_id, **body.model_dump())
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


@router.put("/{project_id}/notes/{note_id}", response_model=NoteOut)
async def update_note(project_id: int, note_id: int, body: NoteIn, db: AsyncSession = Depends(get_db)):
    note = await db.get(ProjectNote, note_id)
    if not note or note.project_id != project_id:
        raise HTTPException(404, "Заметка не найдена")
    for key, value in body.model_dump().items():
        setattr(note, key, value)
    await db.commit()
    await db.refresh(note)
    return note


@router.delete("/{project_id}/notes/{note_id}", status_code=204)
async def delete_note(project_id: int, note_id: int, db: AsyncSession = Depends(get_db)):
    note = await db.get(ProjectNote, note_id)
    if not note or note.project_id != project_id:
        raise HTTPException(404, "Заметка не найдена")
    await db.delete(note)
    await db.commit()


# ---------- chats ----------


@router.put("/{project_id}/chats/{chat_row_id}", response_model=TelegramChatOut)
async def attach_chat(project_id: int, chat_row_id: int, db: AsyncSession = Depends(get_db)):
    await _project_or_404(db, project_id)
    chat = await db.get(TelegramChat, chat_row_id)
    if not chat:
        raise HTTPException(404, "Чат не найден")
    chat.project_id = project_id
    await db.commit()
    await db.refresh(chat)
    return chat


@router.delete("/{project_id}/chats/{chat_row_id}", response_model=TelegramChatOut)
async def detach_chat(project_id: int, chat_row_id: int, db: AsyncSession = Depends(get_db)):
    chat = await db.get(TelegramChat, chat_row_id)
    if not chat or chat.project_id != project_id:
        raise HTTPException(404, "Чат не найден")
    chat.project_id = None
    await db.commit()
    await db.refresh(chat)
    return chat
