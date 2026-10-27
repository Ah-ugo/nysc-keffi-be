import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import db
from ..security import active_stream, hash_pw, rate_limit, require_director, require_member
from ..services import storage
from ..services.audit import audit
from ..services.face import get_provider
from ..services.notify import DIRECTOR, notify
from ..services.rules import image_quality, normalise_callup, valid_callup
from ..util import now, oid, ser
from .hostels import auto_assign_bed
from ..services.platoons import assign_member, parse_state_code

router = APIRouter(prefix="/api", tags=["members"])
MAX_IMG = 6 * 1024 * 1024


async def _read_image(f: UploadFile, label: str) -> bytes:
    data = await f.read()
    if len(data) > MAX_IMG:
        raise HTTPException(413, f"{label} photo is too large (max 6MB)")
    if f.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, f"{label} photo must be JPEG/PNG/WebP")
    ok, why = image_quality(data)
    if not ok:
        raise HTTPException(422, f"{label} photo rejected: {why}")
    ok, why = await get_provider().check_quality(data)
    if not ok:
        raise HTTPException(422, f"{label} photo rejected: {why}")
    return data


@router.post("/register", dependencies=[Depends(rate_limit(8, 300))])
async def register(
    stream_id: str = Form(...), full_name: str = Form(..., min_length=3, max_length=120),
    callup: str = Form(...), state: str = Form(..., max_length=60), state_code: str = Form("", max_length=12), gender: str = Form(...),
    phone: str = Form(..., min_length=7, max_length=20), email: str = Form(""),
    password: str = Form(..., min_length=8, max_length=100), consent: bool = Form(...),
    front: UploadFile = File(...), side: UploadFile = File(...),
):
    """Self-registration via QR. The QR only opens this page; identity is established by the Director's verification."""
    stream = await active_stream(oid(stream_id))
    if not consent:
        raise HTTPException(400, "Biometric consent is required to register")
    if gender not in ("M", "F"):
        raise HTTPException(400, "Gender must be M or F")
    cu = normalise_callup(callup)
    if not valid_callup(cu, settings.callup_regex):
        raise HTTPException(422, "Call-up number format is not valid")
    if await db.members.find_one({"callup": cu}, {"_id": 1}):
        await notify(DIRECTOR, stream["_id"], "Registration exception", "A duplicate call-up registration was attempted.", "registration_exception")
        raise HTTPException(409, "This call-up number is already registered. See the registration desk if this is an error.")
    f, s = await _read_image(front, "Front"), await _read_image(side, "Side-profile")
    if not storage.configured():
        raise HTTPException(503, "Image storage is not configured")
    folder = f"nysc/{stream['label'].replace(' ', '_')}/{cu.replace('/', '_')}"
    fid, sid_ = await storage.upload_private(f, folder), await storage.upload_private(s, folder)
    provider_on = bool(getattr(get_provider(), "available", False))
    doc = {
        "callup": cu, "stream_id": stream["_id"], "stream_label": stream["label"], "full_name": full_name.strip(),
        "state": state.strip(), "state_code": state_code.strip(), "gender": gender, "phone": phone.strip(), "email": email.strip().lower(),
        "password_hash": hash_pw(password), "enrollment": {"front": fid, "side": sid_, "provider": settings.face_provider},
        "registration_status": "pending_verification",  # Director confirms identity vs call-up letter
        "biometric_status": "quality_checked" if provider_on else "pending_review",
        "consent_at": now(), "registered_at": now(), "in_camp": True,
    }
    try:
        inserted = await db.members.insert_one(doc)
        doc["_id"] = inserted.inserted_id
    except Exception:
        await storage.delete(fid); await storage.delete(sid_)
        raise HTTPException(409, "This call-up number is already registered")
    platoon_assignment = await assign_member(doc, "system", "post-registration state-code assignment")
    await notify(cu, stream["_id"], "Registration received", "Your registration was received and awaits verification.", "registration")
    await notify(DIRECTOR, stream["_id"], "New registration", "A new corps member registered and awaits verification.", "registration", dedupe_key=f"reg:{cu}")
    return {"ok": True, "callup": cu, "stream": stream["label"], "registration_status": doc["registration_status"],
            "biometric_status": doc["biometric_status"], "platoon_assignment": "assigned" if platoon_assignment else "awaiting_assignment"}


@router.get("/members/me")
async def my_profile(m=Depends(require_member)):
    assignment = await db.platoon_assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": "active"})
    return {**ser(m), "platoon_assignment": ser(assignment) if assignment else None}


class StateCodeUpdate(BaseModel):
    state_code: str = Field(min_length=1, max_length=6, pattern=r"^\d{1,6}$")


@router.put("/members/me/state-code")
async def update_my_state_code(b: StateCodeUpdate, m=Depends(require_member)):
    stream = await active_stream(m["stream_id"])
    state_code = parse_state_code(b.state_code)
    if not state_code:
        raise HTTPException(422, "Enter the numeric NYSC state code shown on your documents")
    assignment = await db.platoon_assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": {"$in": ["active", "pending"]}})
    if assignment and m.get("state_code") != state_code:
        raise HTTPException(409, "Your platoon assignment has already started. Ask the Camp Director to correct your state code and assignment.")
    await db.members.update_one({"_id": m["_id"]}, {"$set": {"state_code": state_code, "state_code_updated_at": now()}})
    if assignment:
        result = assignment
    else:
        updated_member = {**m, "state_code": state_code}
        result = await assign_member(updated_member, "system", "member added state code after registration")
    await audit(m["callup"], "member.state_code.update", m["callup"], {"state_code": state_code, "assignment_status": result.get("status") if result else "awaiting_assignment"})
    return {"state_code": state_code, "assignment_status": result.get("status") if result else "awaiting_assignment",
            "platoon_number": result.get("platoon_number") if result and result.get("status") == "active" else None}


# ---------------- Director ----------------
@router.get("/members")
async def list_members(stream_id: str | None = None, q: str = "", status: str | None = None,
                       page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=200), u=Depends(require_director)):
    f: dict = {}
    if stream_id:
        f["stream_id"] = oid(stream_id)
    if status:
        f["registration_status"] = status
    if q:
        import re
        r = {"$regex": re.escape(q), "$options": "i"}
        f["$or"] = [{"callup": r}, {"full_name": r}]
    total = await db.members.count_documents(f)
    rows = await db.members.find(f).sort("registered_at", -1).skip((page - 1) * size).limit(size).to_list(size)
    return {"total": total, "items": ser(rows)}


@router.get("/members/{rest:path}")
async def member_detail(rest: str, u=Depends(require_director)):
    if rest.endswith("/photo/front") or rest.endswith("/photo/side"):
        kind = rest.rsplit("/", 1)[-1]
        callup = rest[: -(len("/photo/" + kind))]
        if kind not in ("front", "side"):
            raise HTTPException(404)
        m = await db.members.find_one({"callup": normalise_callup(callup)}, {"enrollment": 1})
        if not m:
            raise HTTPException(404)
        data = await storage.fetch(m["enrollment"][kind])
        await audit(u["sub"], "biometric.view", callup, {"kind": kind})
        return StreamingResponse(io.BytesIO(data), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    m = await db.members.find_one({"callup": normalise_callup(rest)})
    if not m:
        raise HTTPException(404, "Member not found")
    return ser(m)


class VerifyIn(BaseModel):
    decision: str  # verified | rejected
    note: str = ""


class CorrectIn(BaseModel):
    new_callup: str
    reason: str


@router.post("/members/{rest:path}")
async def member_actions(rest: str, request: Request, u=Depends(require_director)):
    if rest.endswith("/verify"):
        callup = rest[: -len("/verify")]
        body = VerifyIn(**(await request.json()))
        if body.decision not in ("verified", "rejected"):
            raise HTTPException(400, "Invalid decision")
        m = await db.members.find_one_and_update({"callup": normalise_callup(callup)}, {"$set": {"registration_status": body.decision, "verified_by": u["sub"], "verified_at": now(), "verification_note": body.note}})
        if not m:
            raise HTTPException(404)
        allocation = None
        if body.decision == "verified":
            allocation = await auto_assign_bed(m, "system")
            if not allocation:
                await notify(DIRECTOR, m["stream_id"], "Bed allocation needed", f"{m['callup']} was verified but no suitable bed is currently available.", "allocation_exception", dedupe_key=f"bed:{m['callup']}")
        await audit(u["sub"], "registration.verify", m["callup"], body.model_dump())
        await notify(m["callup"], m["stream_id"], "Registration update", "Your registration status has changed. Open the app to view.", "registration")
        return {"ok": True, "assignment": ser(allocation) if allocation else None}
    if rest.endswith("/correct-callup"):
        callup = rest[: -len("/correct-callup")]
        data = await request.json()
        b = CorrectIn(**data)
        old, new = normalise_callup(callup), normalise_callup(b.new_callup)
        if not b.reason.strip():
            raise HTTPException(400, "A reason is required")
        if not valid_callup(new, settings.callup_regex):
            raise HTTPException(422, "Invalid call-up format")
        if await db.members.find_one({"callup": new}, {"_id": 1}):
            raise HTTPException(409, "New call-up number already exists")
        m = await db.members.find_one_and_update({"callup": old}, {"$set": {"callup": new}, "$push": {"callup_history": {"old": old, "by": u["sub"], "reason": b.reason, "at": now()}}})
        if not m:
            raise HTTPException(404)
        for col, fld in [("assignments", "callup"), ("beds", "occupant"), ("exits", "callup"), ("attendance", "callup"), ("complaints", "callup"), ("notifications", "recipient")]:
            await db[col].update_many({fld: old}, {"$set": {fld: new}})
        await db.swaps.update_many({"from_callup": old}, {"$set": {"from_callup": new}})
        await db.swaps.update_many({"to_callup": old}, {"$set": {"to_callup": new}})
        await audit(u["sub"], "member.correct_callup", old, {"to": new, "reason": b.reason})
        return {"ok": True}
    raise HTTPException(404)
