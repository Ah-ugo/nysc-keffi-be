from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..db import db
from ..security import current_user
from ..services.notify import DIRECTOR
from ..services.push import enabled
from ..util import now

router = APIRouter(prefix="/api/push", tags=["push"])


@router.get("/key")
async def key(u=Depends(current_user)):
    if not enabled():
        raise HTTPException(503, "Push notifications are not configured on the server")
    return {"key": settings.vapid_public_key}


class Sub(BaseModel):
    endpoint: str
    keys: dict


@router.post("/subscribe")
async def subscribe(b: Sub, u=Depends(current_user)):
    who = DIRECTOR if u["role"] == "director" else u["sub"]
    await db.push_subs.update_one({"subscription.endpoint": b.endpoint}, {"$set": {"recipient": who, "subscription": b.model_dump(), "at": now()}}, upsert=True)
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(b: Sub, u=Depends(current_user)):
    await db.push_subs.delete_one({"subscription.endpoint": b.endpoint})
    return {"ok": True}
