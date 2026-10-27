from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.models import PushSubscription
from app.schemas import PushSubscriptionIn
from app.services import push as svc

router = APIRouter(prefix="/api/push", tags=["push"], dependencies=[Depends(require_auth)])


@router.get("/vapid")
async def vapid_key():
    return {"public_key": get_settings().vapid_public_key}


@router.post("/subscribe", status_code=201)
async def subscribe(body: PushSubscriptionIn, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if existing:
        existing.keys = body.keys
        existing.user_agent = body.user_agent
    else:
        db.add(PushSubscription(**body.model_dump()))
    await db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: PushSubscriptionIn, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test_push(db: AsyncSession = Depends(get_db)):
    sent = await svc.notify_all(db, "Personal Dash", "Пуши работают 🎉", url="/")
    return {"sent": sent}
