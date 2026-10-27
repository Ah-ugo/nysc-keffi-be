import re

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..db import db
from ..security import active_stream, require_director, require_member
from ..services.audit import audit
from ..services.notify import notify
from ..util import now, oid, ser

router = APIRouter(prefix="/api", tags=["hostels"])


def _gender_code(value):
    value = str(value or "").strip().casefold()
    if value in ("m", "male"):
        return "M"
    if value in ("f", "female"):
        return "F"
    return None


def _gender_values(value):
    code = _gender_code(value)
    if code == "M":
        return ["M", "m", "Male", "male"]
    if code == "F":
        return ["F", "f", "Female", "female"]
    return []


class HostelIn(BaseModel):
    stream_id: str
    code: str = Field(pattern=r"^[A-Za-z0-9]{1,10}$")
    name: str = ""
    bunk_count: int = Field(ge=1, le=1000)
    gender: str = Field(pattern="^[MF]$")


@router.post("/hostels")
async def create_hostel(b: HostelIn, u=Depends(require_director)):
    s = await active_stream(oid(b.stream_id))
    code = b.code.upper()
    h = {"stream_id": s["_id"], "code": code, "name": b.name or code, "bunk_count": b.bunk_count, "gender": b.gender, "active": True, "created_at": now()}
    try:
        r = await db.hostels.insert_one(h)
    except Exception:
        raise HTTPException(409, "Hostel code already exists in this stream")
    beds = []
    for i in range(1, b.bunk_count + 1):
        for pos, suf in (("upper", "U"), ("lower", "L")):
            bunk = f"{code}-{i:03d}"
            beds.append({"stream_id": s["_id"], "hostel_id": r.inserted_id, "hostel_code": code, "bunk": bunk, "bunk_no": i,
                         "position": pos, "code": f"{bunk}-{suf}", "status": "available", "occupant": None})
    await db.beds.insert_many(beds)
    await assign_waiting_members(s["_id"])
    await audit(u["sub"], "hostel.create", code, {"stream": s["label"], "bunks": b.bunk_count})
    return ser({**h, "_id": r.inserted_id, "capacity": len(beds)})


@router.get("/hostels")
async def list_hostels(stream_id: str, u=Depends(require_director)):
    sid = oid(stream_id)
    out = []
    async for h in db.hostels.find({"stream_id": sid}).sort("code", 1):
        counts = {x["_id"]: x["n"] async for x in db.beds.aggregate([{"$match": {"hostel_id": h["_id"]}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
        cap = sum(counts.values())
        out.append({**ser(h), "capacity": cap, "occupied": counts.get("occupied", 0), "reserved": counts.get("reserved", 0),
                    "available": counts.get("available", 0), "disabled": counts.get("disabled", 0)})
    return out


class HostelPatch(BaseModel):
    name: str | None = None
    active: bool | None = None


@router.patch("/hostels/{hid}")
async def patch_hostel(hid: str, b: HostelPatch, u=Depends(require_director)):
    h = await db.hostels.find_one({"_id": oid(hid)})
    if not h:
        raise HTTPException(404)
    upd = {}
    if b.name is not None:
        upd["name"] = b.name
    if b.active is not None:
        if b.active is False and await db.beds.count_documents({"hostel_id": h["_id"], "status": {"$in": ["occupied", "reserved"]}}):
            raise HTTPException(409, "Reassign or release occupied/reserved beds before deactivating this hostel")
        upd["active"] = b.active
        await db.beds.update_many({"hostel_id": h["_id"], "status": {"$in": ["available", "disabled"]}}, {"$set": {"status": "available" if b.active else "disabled"}})
    await db.hostels.update_one({"_id": h["_id"]}, {"$set": upd})
    await audit(u["sub"], "hostel.update", h["code"], upd)
    return {"ok": True}


@router.get("/hostels/{hid}/map")
async def hostel_map(hid: str, u=Depends(require_director)):
    """Compact bunk map for the visual bed grid: one entry per bunk with upper/lower status and occupant."""
    h = await db.hostels.find_one({"_id": oid(hid)})
    if not h:
        raise HTTPException(404)
    bunks: dict = {}
    names = {}
    async for b in db.beds.find({"hostel_id": h["_id"]}).sort([("bunk_no", 1)]):
        e = bunks.setdefault(b["bunk_no"], {"bunk": b["bunk"], "upper": None, "lower": None})
        e[b["position"]] = {"code": b["code"], "status": b["status"], "occupant": b["occupant"]}
    occ = [x["occupant"] for v in bunks.values() for x in (v["upper"], v["lower"]) if x and x["occupant"]]
    async for m in db.members.find({"callup": {"$in": occ}}, {"callup": 1, "full_name": 1}):
        names[m["callup"]] = m["full_name"]
    for v in bunks.values():
        for k in ("upper", "lower"):
            if v[k] and v[k]["occupant"]:
                v[k]["name"] = names.get(v[k]["occupant"])
    return {"hostel": h["code"], "gender": h["gender"], "bunks": [bunks[k] for k in sorted(bunks)]}


@router.get("/beds")
async def search_beds(stream_id: str, hostel: str | None = None, status: str | None = None, q: str = "",
                      page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=300), u=Depends(require_director)):
    f: dict = {"stream_id": oid(stream_id)}
    if hostel:
        f["hostel_code"] = hostel.upper()
    if status:
        f["status"] = status
    if q:
        r = {"$regex": re.escape(q), "$options": "i"}
        f["$or"] = [{"code": r}, {"occupant": r}]
        names = await db.members.find({"stream_id": f["stream_id"], "full_name": r}, {"callup": 1}).to_list(200)
        if names:
            f["$or"].append({"occupant": {"$in": [n["callup"] for n in names]}})
    total = await db.beds.count_documents(f)
    rows = await db.beds.find(f).sort([("hostel_code", 1), ("bunk_no", 1), ("position", -1)]).skip((page - 1) * size).limit(size).to_list(size)
    return {"total": total, "items": ser(rows)}


async def assign_bed(member: dict, bed_code: str, actor: str, event: str = "assigned"):
    """Atomic claim: only an 'available' bed in an active hostel can be taken (race-safe via conditional update)."""
    s = await active_stream(member["stream_id"])
    member_gender = _gender_code(member.get("gender"))
    if not member_gender:
        raise HTTPException(409, "Member gender is missing or invalid; update the member record before assigning a bed")
    if await db.assignments.find_one({"stream_id": s["_id"], "callup": member["callup"], "status": "active"}):
        raise HTTPException(409, "Member already has an active bed; use reassign")
    bed = await db.beds.find_one_and_update(
        {"stream_id": s["_id"], "code": bed_code.upper(), "status": "available"},
        {"$set": {"status": "occupied", "occupant": member["callup"]}})
    if not bed:
        raise HTTPException(409, "Bed is not available")
    h = await db.hostels.find_one({"_id": bed["hostel_id"]})
    if not h["active"] or _gender_code(h.get("gender")) != member_gender:
        await db.beds.update_one({"_id": bed["_id"]}, {"$set": {"status": "available", "occupant": None}})
        raise HTTPException(409, "Hostel is inactive or does not match member gender")
    a = {"stream_id": s["_id"], "callup": member["callup"], "bed_code": bed["code"], "hostel_code": bed["hostel_code"],
         "bunk": bed["bunk"], "position": bed["position"], "status": "active", "assigned_at": now(), "assigned_by": actor}
    try:
        await db.assignments.insert_one(a)
    except Exception:
        await db.beds.update_one({"_id": bed["_id"]}, {"$set": {"status": "available", "occupant": None}})
        raise HTTPException(409, "Conflicting assignment; try again")
    await db.allocation_history.insert_one({"stream_id": s["_id"], "callup": member["callup"], "event": event, "bed_code": bed["code"], "by": actor, "at": now()})
    await notify(member["callup"], s["_id"], "Hostel assignment", "Your hostel assignment has been updated. Open the app to view.", "assignment")
    return a


async def auto_assign_bed(member: dict, actor: str = "system"):
    """Assign the first available bed in an active, gender-matched hostel."""
    existing = await db.assignments.find_one(
        {"stream_id": member["stream_id"], "callup": member["callup"], "status": "active"}
    )
    if existing:
        return existing
    s = await active_stream(member["stream_id"])
    gender_values = _gender_values(member.get("gender"))
    if not gender_values:
        raise HTTPException(409, "Member gender is missing or invalid; update the member record before assigning a bed")
    hs = [h["_id"] async for h in db.hostels.find(
        {"stream_id": s["_id"], "gender": {"$in": gender_values}, "active": True}, {"_id": 1}
    )]
    # A concurrent allocator may claim the first bed; try the next one in order.
    while True:
        bed = await db.beds.find_one(
            {"stream_id": s["_id"], "hostel_id": {"$in": hs}, "status": "available"},
            sort=[("hostel_code", 1), ("bunk_no", 1), ("position", -1)],
        )
        if not bed:
            return None
        try:
            return await assign_bed(member, bed["code"], actor, event="auto_assigned")
        except HTTPException as e:
            if e.status_code != 409 or e.detail != "Bed is not available":
                raise


async def assign_waiting_members(stream_id):
    """Backfill verified members who registered before automatic allocation was enabled."""
    async for member in db.members.find({
        "stream_id": stream_id, "registration_status": "verified", "in_camp": True,
    }).sort("registered_at", 1):
        try:
            await auto_assign_bed(member)
        except HTTPException as e:
            if e.status_code != 409:
                raise


async def release_bed(callup: str, stream_id, actor: str, reason: str, event="released"):
    a = await db.assignments.find_one_and_update({"stream_id": stream_id, "callup": callup, "status": "active"},
                                                  {"$set": {"status": "released", "released_at": now(), "release_reason": reason}})
    if not a:
        return None
    await db.beds.update_one({"stream_id": stream_id, "code": a["bed_code"]}, {"$set": {"status": "available", "occupant": None}})
    await db.allocation_history.insert_one({"stream_id": stream_id, "callup": callup, "event": event, "bed_code": a["bed_code"], "by": actor, "reason": reason, "at": now()})
    return a


class ReassignIn(BaseModel):
    callup: str
    reason: str


@router.post("/assignments/reassign")
async def reassign(b: ReassignIn, u=Depends(require_director)):
    m = await db.members.find_one({"callup": b.callup.strip().upper()})
    if not m:
        raise HTTPException(404)
    cur = await db.assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": "active"})
    if cur:
        ex = await db.exits.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": {"$in": ["departed", "overdue"]}, "type": "temporary"})
        if ex:
            raise HTTPException(409, "Bed is reserved for an absent member; resolve the exit first")
    # claim the new bed first so a failure never leaves the member bedless
    s = await active_stream(m["stream_id"])
    gender_values = _gender_values(m.get("gender"))
    if not gender_values:
        raise HTTPException(409, "Member gender is missing or invalid; update the member record before assigning a bed")
    hs = [h["_id"] async for h in db.hostels.find(
        {"stream_id": s["_id"], "gender": {"$in": gender_values}, "active": True}, {"_id": 1}
    )]
    while True:
        candidate = await db.beds.find_one(
            {"stream_id": s["_id"], "hostel_id": {"$in": hs}, "status": "available"},
            sort=[("hostel_code", 1), ("bunk_no", 1), ("position", -1)],
        )
        if not candidate:
            raise HTTPException(409, "No available bed for this member")
        bed = await db.beds.find_one_and_update(
            {"_id": candidate["_id"], "status": "available"},
            {"$set": {"status": "occupied", "occupant": m["callup"]}},
        )
        if bed:
            break
    if cur:
        await db.assignments.update_one({"_id": cur["_id"]}, {"$set": {"status": "released", "released_at": now(), "release_reason": "reassigned"}})
        await db.beds.update_one({"stream_id": s["_id"], "code": cur["bed_code"]}, {"$set": {"status": "available", "occupant": None}})
    a = {"stream_id": s["_id"], "callup": m["callup"], "bed_code": bed["code"], "hostel_code": bed["hostel_code"], "bunk": bed["bunk"],
         "position": bed["position"], "status": "active", "assigned_at": now(), "assigned_by": u["sub"]}
    await db.assignments.insert_one(a)
    await db.allocation_history.insert_one({"stream_id": s["_id"], "callup": m["callup"], "event": "reassigned", "bed_code": bed["code"], "from": cur["bed_code"] if cur else None, "by": u["sub"], "reason": b.reason, "at": now()})
    await audit(u["sub"], "assignment.reassign", m["callup"], {"to": bed["code"], "reason": b.reason})
    await notify(m["callup"], s["_id"], "Hostel assignment", "Your hostel assignment has been updated. Open the app to view.", "assignment")
    return ser(a)


class ReleaseIn(BaseModel):
    callup: str
    reason: str


@router.post("/assignments/release")
async def release(b: ReleaseIn, u=Depends(require_director)):
    m = await db.members.find_one({"callup": b.callup.strip().upper()})
    if not m:
        raise HTTPException(404)
    a = await release_bed(m["callup"], m["stream_id"], u["sub"], b.reason)
    if not a:
        raise HTTPException(404, "No active assignment")
    await audit(u["sub"], "assignment.release", m["callup"], {"bed": a["bed_code"], "reason": b.reason})
    return {"ok": True}


@router.get("/assignments/history")
async def history(callup: str | None = None, stream_id: str | None = None, u=Depends(require_director)):
    f = {}
    if callup:
        f["callup"] = callup.strip().upper()
    if stream_id:
        f["stream_id"] = oid(stream_id)
    return ser(await db.allocation_history.find(f).sort("at", -1).limit(500).to_list(500))


@router.get("/assignments/me")
async def my_assignment(m=Depends(require_member)):
    a = await db.assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": "active"})
    return {"member": {"full_name": m["full_name"], "callup": m["callup"], "stream": m["stream_label"]},
            "assignment": ser(a) if a else None}
