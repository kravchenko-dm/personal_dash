from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.config import get_settings
from app.db import get_db
from app.models import PushSubscription
from app.schemas import PushSubscriptionIn
from app.services import push as svc
from app.services.bot_access import Account

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/vapid", dependencies=[Depends(require_auth)])
async def vapid_key():
    return {"public_key": get_settings().vapid_public_key}


@router.post("/subscribe", status_code=201)
async def subscribe(
    body: PushSubscriptionIn, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    existing = await db.scalar(select(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    if existing:  # the same browser may now belong to someone else
        existing.keys = body.keys
        existing.user_agent = body.user_agent
        existing.user_id = user.id
    else:
        db.add(PushSubscription(**body.model_dump(), user_id=user.id))
    await db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(
    body: PushSubscriptionIn, db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)
):
    await db.execute(delete(PushSubscription).where(PushSubscription.endpoint == body.endpoint))
    await db.commit()
    return {"ok": True}


@router.post("/test")
async def test_push(db: AsyncSession = Depends(get_db), user: Account = Depends(require_auth)):
    sent = await svc.notify_user(db, user.id, "Personal Dash", "Пуши работают 🎉", url="/")
    return {"sent": sent}
