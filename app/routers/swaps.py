from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import db
from ..security import active_stream, rate_limit, require_director, require_member
from ..services.audit import audit
from ..services.notify import DIRECTOR, notify
from ..util import now, oid, ser

router = APIRouter(prefix="/api/swaps", tags=["swaps"])


async def _active(callup, stream_id):
    return await db.assignments.find_one({"stream_id": stream_id, "callup": callup, "status": "active"})


async def _reserved(callup, stream_id):
    return await db.exits.find_one({"stream_id": stream_id, "callup": callup, "type": "temporary", "status": {"$in": ["approved", "departed", "overdue"]}})


class SwapIn(BaseModel):
    friend_callup: str


@router.post("", dependencies=[Depends(rate_limit(10, 60))])
async def request_swap(b: SwapIn, m=Depends(require_member)):
    s = await active_stream(m["stream_id"])
    friend = await db.members.find_one({"callup": b.friend_callup.strip().upper(), "stream_id": s["_id"]})
    if not friend or friend["callup"] == m["callup"]:
        raise HTTPException(404, "No matching corps member in your stream")
    if friend["gender"] != m["gender"]:
        raise HTTPException(409, "Swaps are only allowed within the same hostel gender")
    a1, a2 = await _active(m["callup"], s["_id"]), await _active(friend["callup"], s["_id"])
    if not a1 or not a2:
        raise HTTPException(409, "Both members need an active bed assignment")
    if await _reserved(m["callup"], s["_id"]) or await _reserved(friend["callup"], s["_id"]):
        raise HTTPException(409, "A reserved bed (temporary exit) cannot be swapped")
    dup = await db.swaps.find_one({"stream_id": s["_id"], "status": "pending", "$or": [
        {"from_callup": {"$in": [m["callup"], friend["callup"]]}}, {"to_callup": {"$in": [m["callup"], friend["callup"]]}}]})
    if dup:
        raise HTTPException(409, "One of you already has a pending swap")
    doc = {"stream_id": s["_id"], "from_callup": m["callup"], "to_callup": friend["callup"], "from_bed": a1["bed_code"], "to_bed": a2["bed_code"],
           "consent": {m["callup"]: True, friend["callup"]: None}, "status": "pending", "created_at": now()}
    r = await db.swaps.insert_one(doc)
    await notify(friend["callup"], s["_id"], "Bunk swap request", "A friend asked to swap beds. Open the app to respond.", "swap")
    await notify(DIRECTOR, s["_id"], "Pending bunk swap", "A bunk swap is awaiting consent.", "swap", dedupe_key=f"swap-dir:{r.inserted_id}")
    return ser({**doc, "_id": r.inserted_id})


@router.get("/mine")
async def mine(m=Depends(require_member)):
    return ser(await db.swaps.find({"$or": [{"from_callup": m["callup"]}, {"to_callup": m["callup"]}]}).sort("created_at", -1).to_list(50))


class Respond(BaseModel):
    accept: bool


@router.post("/{swap_id}/respond")
async def respond(swap_id: str, b: Respond, m=Depends(require_member)):
    sw = await db.swaps.find_one({"_id": oid(swap_id)})
    if not sw or sw["to_callup"] != m["callup"] or sw["status"] != "pending":
        raise HTTPException(404, "No pending request for you")
    s = await active_stream(sw["stream_id"])
    if not b.accept:
        await db.swaps.update_one({"_id": sw["_id"]}, {"$set": {"status": "declined", "closed_at": now()}})
        await notify(sw["from_callup"], s["_id"], "Bunk swap result", "Your swap request was declined.", "swap")
        return {"status": "declined"}
    # Re-validate at commit time: beds must still be exactly what both consented to, and not reserved.
    a1, a2 = await _active(sw["from_callup"], s["_id"]), await _active(sw["to_callup"], s["_id"])
    if not a1 or not a2 or a1["bed_code"] != sw["from_bed"] or a2["bed_code"] != sw["to_bed"] or \
            await _reserved(sw["from_callup"], s["_id"]) or await _reserved(sw["to_callup"], s["_id"]):
        await db.swaps.update_one({"_id": sw["_id"]}, {"$set": {"status": "invalid", "closed_at": now()}})
        raise HTTPException(409, "Assignments changed or are reserved; the request is no longer valid")
    await _commit(sw, a1, a2, s["_id"], "member consent")
    return {"status": "completed"}


async def _commit(sw, a1, a2, sid, by):
    """Exchange occupants with conditional updates; compensates if the second step fails."""
    r1 = await db.beds.update_one({"stream_id": sid, "code": a1["bed_code"], "occupant": a1["callup"]}, {"$set": {"occupant": "__swap__"}})
    if not r1.modified_count:
        raise HTTPException(409, "Bed state changed; try again")
    r2 = await db.beds.update_one({"stream_id": sid, "code": a2["bed_code"], "occupant": a2["callup"]}, {"$set": {"occupant": a1["callup"]}})
    if not r2.modified_count:
        await db.beds.update_one({"stream_id": sid, "code": a1["bed_code"]}, {"$set": {"occupant": a1["callup"]}})
        raise HTTPException(409, "Bed state changed; try again")
    await db.beds.update_one({"stream_id": sid, "code": a1["bed_code"]}, {"$set": {"occupant": a2["callup"]}})
    for a, new in ((a1, a2), (a2, a1)):
        await db.assignments.update_one({"_id": a["_id"]}, {"$set": {"status": "released", "released_at": now(), "release_reason": "swap"}})
    for owner, bed in ((a1["callup"], a2), (a2["callup"], a1)):
        await db.assignments.insert_one({"stream_id": sid, "callup": owner, "bed_code": bed["bed_code"], "hostel_code": bed["hostel_code"], "bunk": bed["bunk"],
                                         "position": bed["position"], "status": "active", "assigned_at": now(), "assigned_by": "swap"})
        await db.allocation_history.insert_one({"stream_id": sid, "callup": owner, "event": "swapped", "bed_code": bed["bed_code"], "by": by, "at": now()})
    await db.swaps.update_one({"_id": sw["_id"]}, {"$set": {"status": "completed", "closed_at": now(), "consent." + sw["to_callup"]: True}})
    for c in (a1["callup"], a2["callup"]):
        await notify(c, sid, "Bunk swap result", "Your bunk swap was completed. Open the app to view.", "swap")
    await audit(by, "swap.commit", sw["from_callup"], {"with": sw["to_callup"]})


@router.post("/{swap_id}/cancel")
async def cancel(swap_id: str, m=Depends(require_member)):
    r = await db.swaps.update_one({"_id": oid(swap_id), "from_callup": m["callup"], "status": "pending"}, {"$set": {"status": "cancelled", "closed_at": now()}})
    if not r.modified_count:
        raise HTTPException(404)
    return {"status": "cancelled"}


@router.get("")
async def all_swaps(stream_id: str, u=Depends(require_director)):
    return ser(await db.swaps.find({"stream_id": oid(stream_id)}).sort("created_at", -1).limit(300).to_list(300))
