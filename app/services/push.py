"""Web Push. Payloads are deliberately generic (lock-screen safe): title + 'Open the app'."""
import asyncio
import json

from ..config import settings
from ..db import db


def enabled() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


def _send(sub: dict, payload: dict):
    from pywebpush import webpush
    webpush(subscription_info=sub, data=json.dumps(payload), vapid_private_key=settings.vapid_private_key, vapid_claims={"sub": settings.vapid_subject})


async def push(recipient: str, title: str) -> bool | None:
    """Returns None if push not configured / no subscriptions, else True/False overall delivery."""
    if not enabled():
        return None
    subs = await db.push_subs.find({"recipient": recipient}).to_list(10)
    if not subs:
        return None
    ok = False
    for s in subs:
        try:
            await asyncio.to_thread(_send, s["subscription"], {"title": title, "body": "Open the app to view details."})
            ok = True
        except Exception as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                await db.push_subs.delete_one({"_id": s["_id"]})
    return ok
