from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import db
from ..security import active_stream, current_user, require_director, require_member
from ..services.audit import audit
from ..services.notify import DIRECTOR, notify
from ..util import now, oid, ser

router = APIRouter(prefix="/api", tags=["engagement"])
CATEGORIES = ["accommodation", "feeding", "sanitation", "welfare", "security", "general"]
CSTATUS = ["open", "in_review", "resolved", "closed"]


class ComplaintIn(BaseModel):
    kind: str = "complaint"  # complaint | suggestion
    category: str
    subject: str
    body: str
    private: bool = True
    anonymous: bool = False  # only honoured for suggestions


@router.post("/complaints")
async def submit_complaint(b: ComplaintIn, m=Depends(require_member)):
    s = await active_stream(m["stream_id"])
    if b.category not in CATEGORIES or b.kind not in ("complaint", "suggestion"):
        raise HTTPException(400, "Invalid category or type")
    anon = b.anonymous and b.kind == "suggestion"
    d = {"stream_id": s["_id"], "callup": None if anon else m["callup"], "anonymous": anon, "kind": b.kind, "category": b.category,
         "subject": b.subject.strip()[:150], "body": b.body.strip()[:3000], "private": b.private, "status": "open", "responses": [], "created_at": now()}
    r = await db.complaints.insert_one(d)
    await notify(DIRECTOR, s["_id"], "New complaint", "A new complaint or suggestion was submitted.", "complaint")
    return {"id": str(r.inserted_id)}


@router.get("/complaints/mine")
async def my_complaints(m=Depends(require_member)):
    return ser(await db.complaints.find({"callup": m["callup"]}).sort("created_at", -1).to_list(100))


@router.get("/complaints")
async def list_complaints(stream_id: str, status: str | None = None, category: str | None = None, u=Depends(require_director)):
    f: dict = {"stream_id": oid(stream_id)}
    if status:
        f["status"] = status
    if category:
        f["category"] = category
    return ser(await db.complaints.find(f).sort("created_at", -1).to_list(500))


class Respond(BaseModel):
    status: str
    response: str = ""
    assigned_to: str | None = None
    resolution: str | None = None


@router.post("/complaints/{cid}/respond")
async def respond(cid: str, b: Respond, u=Depends(require_director)):
    if b.status not in CSTATUS:
        raise HTTPException(400, "Invalid status")
    c = await db.complaints.find_one({"_id": oid(cid)})
    if not c:
        raise HTTPException(404)
    upd = {"status": b.status}
    if b.assigned_to is not None:
        upd["assigned_to"] = b.assigned_to
    if b.resolution:
        upd["resolution"] = b.resolution
    if b.status in ("resolved", "closed"):
        upd["resolved_at"] = now()
    push = {"responses": {"by": u["sub"], "text": b.response, "at": now()}} if b.response else {}
    await db.complaints.update_one({"_id": c["_id"]}, {"$set": upd, **({"$push": push} if push else {})})
    await audit(u["sub"], "complaint.respond", cid, {"status": b.status})
    if c.get("callup"):
        await notify(c["callup"], c["stream_id"], "Complaint update", "Your complaint has an update. Open the app to view.", "complaint")
    return {"ok": True}


# ---- announcements ----
class AnnIn(BaseModel):
    stream_id: str
    title: str
    body: str


@router.post("/announcements")
async def publish(b: AnnIn, u=Depends(require_director)):
    s = await active_stream(oid(b.stream_id))
    r = await db.announcements.insert_one({"stream_id": s["_id"], "title": b.title.strip(), "body": b.body.strip(), "by": u["sub"], "created_at": now()})
    n = 0
    async for m in db.members.find({"stream_id": s["_id"]}, {"callup": 1}):
        await notify(m["callup"], s["_id"], "New announcement", "The Camp Director posted an announcement.", "announcement", dedupe_key=f"ann:{r.inserted_id}:{m['callup']}")
        n += 1
    await audit(u["sub"], "announcement.publish", b.title, {"recipients": n})
    return {"id": str(r.inserted_id), "recipients": n}


@router.get("/announcements")
async def announcements(m=Depends(require_member)):
    rows = await db.announcements.find({"stream_id": m["stream_id"]}).sort("created_at", -1).to_list(100)
    read = {x["announcement_id"] async for x in db.announcement_reads.find({"callup": m["callup"]})}
    return [{**ser(r), "read": r["_id"] in read} for r in rows]


@router.post("/announcements/{aid}/read")
async def mark_read(aid: str, m=Depends(require_member)):
    await db.announcement_reads.update_one({"announcement_id": oid(aid), "callup": m["callup"]}, {"$set": {"at": now()}}, upsert=True)
    return {"ok": True}


@router.get("/announcements/{aid}/stats")
async def ann_stats(aid: str, u=Depends(require_director)):
    a = await db.announcements.find_one({"_id": oid(aid)})
    return {"read": await db.announcement_reads.count_documents({"announcement_id": oid(aid)}), "audience": await db.members.count_documents({"stream_id": a["stream_id"]})}


# ---- notification inbox ----
@router.get("/notifications")
async def inbox(u=Depends(current_user), page: int = Query(1, ge=1)):
    who = DIRECTOR if u["role"] == "director" else u["sub"]
    rows = await db.notifications.find({"recipient": who}).sort("created_at", -1).skip((page - 1) * 30).limit(30).to_list(30)
    return {"unread": await db.notifications.count_documents({"recipient": who, "read": False}), "items": ser(rows)}


@router.post("/notifications/read-all")
async def read_all(u=Depends(current_user)):
    who = DIRECTOR if u["role"] == "director" else u["sub"]
    await db.notifications.update_many({"recipient": who, "read": False}, {"$set": {"read": True}})
    return {"ok": True}
