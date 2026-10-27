import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from ..db import db
from ..security import hash_pw, require_director, require_member, require_platoon_officer
from ..services.audit import audit
from ..services.platoons import activate_legacy_pending_assignments, assign_member, ensure_platoons
from ..util import now, oid, ser

router = APIRouter(prefix="/api/platoons", tags=["platoons"])


class MappingIn(BaseModel):
    stream_id: str
    digit_mapping: dict[str, int]


@router.get("")
async def overview(stream_id: str, u=Depends(require_director)):
    sid = oid(stream_id)
    stream = await db.streams.find_one({"_id": sid})
    if not stream:
        raise HTTPException(404, "Stream not found")
    if stream["status"] != "closed":
        await ensure_platoons(sid)
    await activate_legacy_pending_assignments(sid)
    cfg = await db.platoon_configs.find_one({"stream_id": sid})
    rows = []
    for p in await db.platoons.find({"stream_id": sid}).sort("number", 1).to_list(10):
        assignments = db.platoon_assignments
        rows.append({**ser(p),
                     "assigned_members": await assignments.count_documents({"stream_id": sid, "platoon_number": p["number"], "status": "active"}),
                     "pending_confirmations": await db.exits.count_documents({"stream_id": sid, "status": "awaiting_platoon_confirmation", "platoon_assignment_snapshot.platoon_number": p["number"]})})
    awaiting = await db.members.find({"stream_id": sid, "platoon_assignment_status": "awaiting_assignment"}, {"callup": 1, "full_name": 1, "state_code": 1, "platoon_exception": 1}).to_list(1000)
    return {"platoons": rows, "digit_mapping": (cfg or {}).get("digit_mapping", {}),
            "awaiting_assignment": ser(awaiting),
            "assignment_exceptions": ser(await db.platoon_assignment_exceptions.find({"stream_id": sid, "resolved": False}).sort("created_at", -1).limit(500).to_list(500))}


@router.put("/config")
async def save_mapping(b: MappingIn, u=Depends(require_director)):
    sid = oid(b.stream_id)
    stream = await db.streams.find_one({"_id": sid})
    if not stream:
        raise HTTPException(404, "Stream not found")
    if stream["status"] == "closed":
        raise HTTPException(409, "Closed streams are read-only")
    for digit, number in b.digit_mapping.items():
        if digit not in {str(i) for i in range(10)} or number not in range(1, 11):
            raise HTTPException(422, "State-code mapping must use final digits 0–9 and Platoons 1–10")
    await ensure_platoons(sid)
    old = await db.platoon_configs.find_one({"stream_id": sid})
    version = (old or {}).get("version", 0) + 1
    await db.platoon_configs.update_one({"stream_id": sid}, {"$set": {"digit_mapping": b.digit_mapping,
        "version": version, "updated_by": u["sub"], "updated_at": now()},
        "$unset": {"require_director_confirmation": ""},
        "$setOnInsert": {"stream_id": sid, "created_at": now()}}, upsert=True)
    await audit(u["sub"], "platoon.mapping.update", str(sid), {"mapping": b.digit_mapping, "version": version})
    async for member in db.members.find({"stream_id": sid, "platoon_assignment_status": "awaiting_assignment"}):
        await assign_member(member, "system", "assigned after Camp Director updated the mapping")
    return {"ok": True, "version": version}


class PlatoonUpdate(BaseModel):
    stream_id: str
    officer_username: str | None = None
    officer_name: str = ""
    officer_password: str | None = None
    oic_parade_name: str | None = None
    platoon_leader_callup: str | None = None
    appointment_notes: str = ""


@router.put("/{number}")
async def update_platoon(number: int, b: PlatoonUpdate, u=Depends(require_director)):
    if number not in range(1, 11):
        raise HTTPException(404, "Platoon not found")
    sid = oid(b.stream_id)
    stream = await db.streams.find_one({"_id": sid})
    if not stream or stream["status"] == "closed":
        raise HTTPException(409, "Choose an open stream")
    await ensure_platoons(sid)
    platoon = await db.platoons.find_one({"stream_id": sid, "number": number})
    old_username = platoon.get("officer_username")
    username = b.officer_username.strip().lower() if b.officer_username and b.officer_username.strip() else None
    officer_values = None
    if username:
        owner = await db.users.find_one({"username": username, "role": "platoon_officer", "platoon_id": {"$ne": platoon["_id"]}})
        if owner:
            raise HTTPException(409, "That officer account is assigned to another platoon")
        account = await db.users.find_one({"username": username})
        if account and account.get("role") not in ("platoon_officer",):
            raise HTTPException(409, "That username is already used by another account")
        if not account and (not b.officer_password or len(b.officer_password) < 8):
            raise HTTPException(422, "A new officer account needs a password with at least 8 characters")
        values = {"username": username, "role": "platoon_officer", "active": True, "platoon_id": platoon["_id"],
                  "display_name": b.officer_name.strip(), "updated_at": now()}
        if b.officer_password:
            if len(b.officer_password) < 8:
                raise HTTPException(422, "Officer password must contain at least 8 characters")
            values["password_hash"] = hash_pw(b.officer_password)
        officer_values = values
    appointment = platoon.get("appointments", {})
    oic_name = b.oic_parade_name.strip() if b.oic_parade_name is not None else (appointment.get("oic_parade") or {}).get("name", "")
    leader = appointment.get("platoon_leader")
    if b.platoon_leader_callup is not None:
        cu = b.platoon_leader_callup.strip().upper() or None
        leader = None
        if cu:
            member = await db.members.find_one({"stream_id": sid, "callup": cu})
            assigned = await db.platoon_assignments.find_one({"stream_id": sid, "callup": cu, "status": "active", "platoon_number": number})
            if not member or not assigned:
                raise HTTPException(422, "The Platoon Leader must be a corps member currently assigned to this platoon")
            leader = {"callup": cu, "name": member["full_name"]}
    appointment_doc = {"oic_parade": {"name": oic_name, "recorded_by": u["sub"], "updated_at": now()} if oic_name else None,
                       "platoon_leader": ({**leader, "recorded_by": u["sub"], "updated_at": now()} if leader else None),
                       "notes": b.appointment_notes.strip()}
    if officer_values:
        await db.users.update_one({"username": username}, {"$set": officer_values}, upsert=True)
    if old_username and old_username != username:
        await db.users.update_one({"username": old_username, "platoon_id": platoon["_id"]},
                                  {"$set": {"active": False, "updated_at": now()}, "$unset": {"platoon_id": ""}})
    change = {"at": now(), "by": u["sub"], "from_officer": old_username, "to_officer": username,
              "from_appointments": appointment, "to_appointments": appointment_doc}
    await db.platoons.update_one({"_id": platoon["_id"]}, {"$set": {"officer_username": username,
        "officer_name": b.officer_name.strip() if username else "", "appointments": appointment_doc, "updated_at": now()},
        "$push": {"history": change}})
    await audit(u["sub"], "platoon.update", f"{sid}:{number}", {"officer": username, "appointments": appointment_doc})
    return {"ok": True}


@router.get("/{number}/history")
async def platoon_history(number: int, stream_id: str, u=Depends(require_director)):
    if number not in range(1, 11):
        raise HTTPException(404, "Platoon not found")
    platoon = await db.platoons.find_one({"stream_id": oid(stream_id), "number": number}, {"history": 1})
    if not platoon:
        raise HTTPException(404, "Platoon not found")
    return ser(platoon.get("history", []))


@router.get("/members")
async def list_platoon_members(stream_id: str, platoon_number: int, q: str = "", page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200), u=Depends(require_director)):
    if platoon_number not in range(1, 11):
        raise HTTPException(404, "Platoon not found")
    sid = oid(stream_id)
    f = {"stream_id": sid, "platoon_number": platoon_number, "status": "active"}
    if q:
        import re
        rx = {"$regex": re.escape(q), "$options": "i"}
        matching = await db.members.find({"stream_id": sid, "$or": [{"callup": rx}, {"full_name": rx}]}, {"callup": 1}).limit(5000).to_list(5000)
        f["callup"] = {"$in": [m["callup"] for m in matching]}
    total = await db.platoon_assignments.count_documents(f)
    assignments = await db.platoon_assignments.find(f).sort("callup", 1).skip((page - 1) * size).limit(size).to_list(size)
    calls = [a["callup"] for a in assignments]
    mf = {"stream_id": sid, "callup": {"$in": calls}}
    members = await db.members.find(mf, {"callup": 1, "full_name": 1, "state": 1, "state_code": 1, "gender": 1, "phone": 1, "email": 1}).sort("full_name", 1).to_list(size)
    return {"total": total, "items": ser(members)}


@router.get("/history")
async def assignment_history(stream_id: str, callup: str | None = None, u=Depends(require_director)):
    f = {"stream_id": oid(stream_id), "event": {"$in": ["platoon_assigned", "platoon_reassigned"]}}
    if callup:
        f["callup"] = callup.strip().upper()
    return ser(await db.allocation_history.find(f).sort("at", -1).limit(1000).to_list(1000))


@router.get("/roster.csv")
async def export_roster(stream_id: str, platoon_number: int | None = None, u=Depends(require_director)):
    sid = oid(stream_id)
    f = {"stream_id": sid, "status": "active"}
    if platoon_number is not None:
        if platoon_number not in range(1, 11):
            raise HTTPException(422, "Choose a platoon from 1 to 10")
        f["platoon_number"] = platoon_number
    assignments = await db.platoon_assignments.find(f).sort([("platoon_number", 1), ("callup", 1)]).limit(10000).to_list(10000)
    calls = [a["callup"] for a in assignments]
    members = {m["callup"]: m async for m in db.members.find({"stream_id": sid, "callup": {"$in": calls}}, {"callup": 1, "full_name": 1, "state": 1, "gender": 1})}
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Platoon", "Call-up number", "Name", "Gender", "State of deployment", "Assigned at"])
    for a in assignments:
        m = members.get(a["callup"], {})
        row = [a["platoon_number"], a["callup"], m.get("full_name", ""), m.get("gender", ""), m.get("state", ""), a["assigned_at"].isoformat()]
        writer.writerow([("'" + x if isinstance(x, str) and x.startswith(("=", "+", "-", "@", "\t", "\r")) else x) for x in row])
    await audit(u["sub"], "platoon.roster.export", str(sid), {"platoon": platoon_number, "rows": len(assignments)})
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=platoon-roster.csv"})


@router.get("/officer/members")
async def officer_members(q: str = "", u=Depends(require_platoon_officer)):
    platoon = await db.platoons.find_one({"_id": u["platoon_id"]})
    if not platoon:
        raise HTTPException(403, "Your platoon assignment is no longer active")
    f = {"stream_id": platoon["stream_id"], "platoon_number": platoon["number"], "status": "active"}
    calls = [x["callup"] async for x in db.platoon_assignments.find(f, {"callup": 1})]
    mf = {"stream_id": platoon["stream_id"], "callup": {"$in": calls}}
    if q:
        import re
        rx = {"$regex": re.escape(q), "$options": "i"}
        mf["$or"] = [{"callup": rx}, {"full_name": rx}]
    rows = await db.members.find(mf, {"callup": 1, "full_name": 1, "gender": 1, "state": 1}).sort("full_name", 1).to_list(1000)
    return {"platoon_number": platoon["number"], "members": ser(rows)}


@router.get("/me")
async def my_platoon(m=Depends(require_member)):
    await activate_legacy_pending_assignments(m["stream_id"], m["callup"])
    a = await db.platoon_assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": "active"})
    if not a:
        return {"status": m.get("platoon_assignment_status", "awaiting_assignment"), "platoon": None, "reason": m.get("platoon_exception")}
    p = await db.platoons.find_one({"_id": a["platoon_id"]})
    return {"status": "confirmed", "platoon": {"number": a["platoon_number"], "officer_name": p.get("officer_name") if p else None}}
