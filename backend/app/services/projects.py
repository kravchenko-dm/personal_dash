import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Feature, Project, ProjectNote, TelegramChat

STAGES = ("idea", "dev", "beta", "prod", "paused")
FEATURE_STATUSES = ("idea", "planned", "in_progress", "testing", "done")


def slugify(name: str) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeejzijklmnoprstufhccss_y_eua")
    base = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(table)).strip("-")
    return base[:50] or "project"


async def unique_slug(db: AsyncSession, name: str, exclude_id: int | None = None) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while True:
        stmt = select(Project.id).where(Project.slug == slug)
        if exclude_id:
            stmt = stmt.where(Project.id != exclude_id)
        if await db.scalar(stmt) is None:
            return slug
        slug = f"{base}-{n}"
        n += 1


async def feature_counts(db: AsyncSession, project_ids: list[int]) -> dict[int, dict[str, int]]:
    if not project_ids:
        return {}
    stmt = (
        select(Feature.project_id, Feature.status, func.count())
        .where(Feature.project_id.in_(project_ids))
        .group_by(Feature.project_id, Feature.status)
    )
    out: dict[int, dict[str, int]] = {pid: dict.fromkeys(FEATURE_STATUSES, 0) for pid in project_ids}
    for pid, status, count in (await db.execute(stmt)).all():
        out[pid][status] = count
    return out


async def chats_for_project(db: AsyncSession, project_id: int) -> list[TelegramChat]:
    stmt = select(TelegramChat).where(TelegramChat.project_id == project_id).order_by(TelegramChat.title)
    return list((await db.scalars(stmt)).all())


async def next_position(db: AsyncSession, project_id: int, status: str) -> int:
    current = await db.scalar(
        select(func.max(Feature.position)).where(Feature.project_id == project_id, Feature.status == status)
    )
    return (current or 0) + 1


async def move_feature(db: AsyncSession, feature: Feature, status: str, index: int | None) -> Feature:
    """Put the card into `status` column at `index` (None = end) and renumber that column."""
    stmt = (
        select(Feature)
        .where(Feature.project_id == feature.project_id, Feature.status == status, Feature.id != feature.id)
        .order_by(Feature.position, Feature.id)
    )
    column = list((await db.scalars(stmt)).all())
    if index is None or index > len(column):
        index = len(column)
    column.insert(max(index, 0), feature)
    for pos, card in enumerate(column, start=1):
        card.position = pos
    if feature.status != status:
        feature.status = status
        feature.done_at = datetime.now(timezone.utc) if status == "done" else None
    await db.commit()
    await db.refresh(feature)
    return feature


def open_features_text(features: list[Feature], limit: int = 25) -> str:
    rows = [f for f in features if f.status != "done"][:limit]
    if not rows:
        return "Открытых фич нет."
    names = {"idea": "идея", "planned": "план", "in_progress": "в работе", "testing": "тест"}
    return "\n".join(f"- [{names.get(f.status, f.status)}] {f.title}" for f in rows)


async def project_features(db: AsyncSession, project_id: int) -> list[Feature]:
    stmt = (
        select(Feature)
        .where(Feature.project_id == project_id)
        .order_by(Feature.status, Feature.position, Feature.id)
    )
    return list((await db.scalars(stmt)).all())


async def project_notes(db: AsyncSession, project_id: int) -> list[ProjectNote]:
    stmt = (
        select(ProjectNote)
        .where(ProjectNote.project_id == project_id)
        .order_by(ProjectNote.pinned.desc(), ProjectNote.position, ProjectNote.updated_at.desc())
    )
    return list((await db.scalars(stmt)).all())
