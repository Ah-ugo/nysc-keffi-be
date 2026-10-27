"""Lightweight background loop (single instance). For multi-instance deploys use a real scheduler/worker."""
import asyncio
from datetime import timedelta

from .db import db
from .routers.exits import mark_overdue
from .routers.hostels import assign_waiting_members
from .services import storage
from .services.notify import notify
from .util import now


async def tick():
    await mark_overdue()
    async for s in db.streams.find({"status": "active"}, {"_id": 1}):
        await assign_waiting_members(s["_id"])
    t = now()
    # attendance opening reminders (deduped per member+week)
    async for s in db.attendance_schedules.find({"opens_at": {"$lte": t}, "closes_at": {"$gte": t}}):
        async for m in db.members.find({"stream_id": s["stream_id"], "in_camp": True}, {"callup": 1}):
            await notify(m["callup"], s["stream_id"], "Attendance open", "Weekly attendance is now open.", "attendance_open", dedupe_key=f"att-open:{s['_id']}:{m['callup']}")
    # biometric retention: purge expired attendance selfies
    async for a in db.attendance.find({"selfie_public_id": {"$exists": True}, "selfie_expires_at": {"$lt": t}}).limit(100):
        try:
            await storage.delete(a["selfie_public_id"])
            await db.attendance.update_one({"_id": a["_id"]}, {"$unset": {"selfie_public_id": ""}, "$set": {"selfie_purged_at": t}})
        except Exception:
            pass


async def loop():
    while True:
        try:
            await tick()
        except Exception as e:  # never crash the loop
            print("job error", type(e).__name__)
        await asyncio.sleep(300)
