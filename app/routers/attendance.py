from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import io

from ..config import settings
from ..db import db
from ..security import rate_limit, require_director, require_member
from ..services import storage
from ..services.audit import audit
from ..services.face import get_provider
from ..services.notify import DIRECTOR, notify
from ..services.rules import decide_face, evaluate_location, final_status, image_quality
from ..util import aware, now, oid, ser
import secrets
import jwt
from datetime import timedelta
from .streams import get_camp_config

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


class ScheduleIn(BaseModel):
    stream_id: str
    week: int
    label: str = ""
    opens_at: datetime
    closes_at: datetime


@router.post("/schedules")
async def create_schedule(b: ScheduleIn, u=Depends(require_director)):
    if aware(b.closes_at) <= aware(b.opens_at):
        raise HTTPException(422, "Closing time must be after opening time")
    d = {"stream_id": oid(b.stream_id), "week": b.week, "label": b.label or f"Week {b.week}", "opens_at": aware(b.opens_at), "closes_at": aware(b.closes_at), "finalized": False}
    try:
        r = await db.attendance_schedules.insert_one(d)
    except Exception:
        raise HTTPException(409, "A schedule for this week already exists")
    await audit(u["sub"], "attendance.schedule", str(r.inserted_id), {"week": b.week})
    return ser({**d, "_id": r.inserted_id})


@router.get("/schedules")
async def schedules(stream_id: str, u=Depends(require_director)):
    return ser(await db.attendance_schedules.find({"stream_id": oid(stream_id)}).sort("week", -1).to_list(100))


@router.get("/current")
async def current(m=Depends(require_member)):
    t = now()
    s = await db.attendance_schedules.find_one({"stream_id": m["stream_id"], "opens_at": {"$lte": t}, "closes_at": {"$gte": t}})
    if not s:
        return {"open": False}
    rec = await db.attendance.find_one({"schedule_id": s["_id"], "callup": m["callup"]})
    return {"open": True, "schedule": ser(s), "submission": ser(rec) if rec else None}


@router.get("/mine")
async def mine(m=Depends(require_member)):
    rows = await db.attendance.find({"callup": m["callup"]}).sort("week", -1).to_list(100)
    # members see outcome + reasons, never raw scores/coordinates of others, and never image refs
    return [{"week": r["week"], "status": r["final_status"], "source": r["status_source"], "submitted_at": r.get("submitted_at").isoformat() if r.get("submitted_at") else None} for r in rows]


@router.get("/challenge")
async def challenge(m=Depends(require_member)):
    """Single-use liveness challenge token (2 minutes). Only meaningful when LIVENESS_MODE=pose_challenge."""
    if settings.liveness_mode != "pose_challenge" or settings.face_provider != "rekognition":
        return {"required": False}
    tok = jwt.encode({"sub": m["callup"], "purpose": "liveness", "nonce": secrets.token_urlsafe(12), "exp": now() + timedelta(minutes=2)}, settings.jwt_secret, algorithm="HS256")
    return {"required": True, "token": tok, "instruction": "Take photo 1 facing the camera. Then turn your head to one side and take photo 2."}


@router.post("/submit", dependencies=[Depends(rate_limit(6, 300))])
async def submit(schedule_id: str = Form(...), selfie: UploadFile = File(...),
                 lat: float | None = Form(None), lon: float | None = Form(None), accuracy: float | None = Form(None),
                 challenge_selfie: UploadFile | None = File(None), challenge_token: str | None = Form(None),
                 m=Depends(require_member)):
    t = now()
    sch = await db.attendance_schedules.find_one({"_id": oid(schedule_id), "stream_id": m["stream_id"]})
    if not sch or not (sch["opens_at"] <= t <= sch["closes_at"]):
        raise HTTPException(409, "Attendance is not open")
    stream = await db.streams.find_one({"_id": m["stream_id"]})
    if stream["status"] != "active":
        raise HTTPException(409, "Stream is not active")
    if await db.attendance.find_one({"schedule_id": sch["_id"], "callup": m["callup"]}, {"_id": 1}):
        raise HTTPException(409, "Attendance already submitted for this week")
    if m.get("registration_status") == "rejected" or not m.get("enrollment"):
        raise HTTPException(409, "Your registration is not eligible for attendance")
    live = await selfie.read()
    if len(live) > 6 * 1024 * 1024 or selfie.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(415, "Invalid image")
    ok, why = image_quality(live)
    if not ok:  # unusable image: ask to retry, do not consume the weekly submission
        raise HTTPException(422, f"Photo unusable: {why}")
    try:
        refs = [await storage.fetch(m["enrollment"]["front"]), await storage.fetch(m["enrollment"]["side"])]
    except Exception:
        raise HTTPException(503, "Could not load your reference photos; try again shortly")

    # 0) optional liveness (two-frame head-turn challenge)
    liveness_state, liveness_ok = "not_checked", None
    if settings.liveness_mode == "pose_challenge" and settings.face_provider == "rekognition":
        if not challenge_selfie or not challenge_token:
            raise HTTPException(422, "Liveness challenge required: take both photos")
        try:
            tp = jwt.decode(challenge_token, settings.jwt_secret, algorithms=["HS256"])
            assert tp["purpose"] == "liveness" and tp["sub"] == m["callup"]
        except Exception:
            raise HTTPException(422, "Liveness challenge expired; start again")
        try:
            await db.liveness_nonces.insert_one({"nonce": tp["nonce"], "at": now()})
        except Exception:
            raise HTTPException(409, "Challenge already used; start again")
        turned = await challenge_selfie.read()
        liveness_ok, lreason = await get_provider().liveness(live, turned)
        liveness_state = {True: "passed", False: "failed", None: "unavailable"}[liveness_ok]

    # 1) FACE FIRST
    out = await get_provider().compare(refs, live)
    if settings.liveness_mode == "pose_challenge" and settings.face_provider == "rekognition":
        out.liveness_ok = liveness_ok
        if liveness_ok is None:
            out.configured = False
            out.reason = lreason
    match_threshold = settings.local_face_match_threshold if out.provider == "opencv_sface" else settings.face_match_threshold
    review_threshold = match_threshold if out.provider == "opencv_sface" else settings.face_review_threshold
    if not out.configured:
        face_res, face_reason = "review", "Automatic face check is unavailable; Camp Director review is required"
    elif not out.quality_ok:
        face_res, face_reason = "review", out.reason
    elif out.provider == "opencv_sface":
        if out.score is not None and out.score >= match_threshold:
            face_res, face_reason = "passed", f"Face matched the enrolment photos (similarity {out.score:.3f})"
        else:
            face_res, face_reason = "review", "Face match was inconclusive; Camp Director review is required"
    else:
        face_res, face_reason = decide_face(out.score, out.quality_ok, out.liveness_ok, match_threshold, review_threshold)
    # 2) LOCATION ONLY IF FACE PASSED
    if face_res == "passed":
        cfg = await get_camp_config()
        lr = evaluate_location(lat, lon, accuracy, cfg)
        loc = {"result": lr.result, "lat": lat, "lon": lon, "accuracy_m": accuracy, "distance_m": lr.distance_m, "reason": lr.reason}
    else:
        loc = {"result": "not_evaluated", "lat": None, "lon": None, "accuracy_m": None, "distance_m": None,
               "reason": "Location is checked only after an automatic face match; this submission needs face review first"}
    status = final_status(face_res, loc["result"] if loc["result"] != "not_evaluated" else "passed")

    try:
        sid = await storage.upload_private(live, f"nysc/attendance/{sch['_id']}")
    except Exception:
        raise HTTPException(503, "Could not store evidence; try again")
    doc = {"schedule_id": sch["_id"], "stream_id": m["stream_id"], "callup": m["callup"], "week": sch["week"], "submitted_at": t,
           "face": {"provider": out.provider, "score": out.score, "result": face_res, "reason": face_reason, "quality_ok": out.quality_ok, "liveness": liveness_state,
                    "thresholds": {"match": match_threshold if out.configured else None, "review": review_threshold if out.configured else None}},
           "location": loc, "final_status": status, "status_source": "automatic", "selfie_public_id": sid,
           "selfie_expires_at": t + timedelta(days=settings.selfie_retention_days), "overrides": []}
    try:
        await db.attendance.insert_one(doc)
    except Exception:
        await storage.delete(sid)
        raise HTTPException(409, "Attendance already submitted for this week")
    if status == "pending_manual_review":
        await notify(DIRECTOR, m["stream_id"], "Attendance needs review", "An attendance submission requires manual review.", "attendance_review")
    msg = {"verified_present": "Attendance verified.", "pending_manual_review": "Submitted. Your attendance is awaiting manual review.",
           "face_failed": "Face verification did not match. Report to the Camp Director's office.", "location_failed": "You appear to be outside the camp area. Report to the Camp Director's office."}[status]
    if status == "pending_manual_review" and face_res == "review" and not out.configured:
        msg = "Submitted. Automatic face checking is unavailable, so the Camp Director will review your attendance."
    await notify(m["callup"], m["stream_id"], "Attendance received", msg, "attendance")
    return {"status": status, "message": msg}


# ---------------- Director ----------------
@router.get("")
async def list_attendance(stream_id: str, week: int | None = None, status: str | None = None, q: str = "",
                          page: int = Query(1, ge=1), size: int = Query(50, le=200), u=Depends(require_director)):
    f: dict = {"stream_id": oid(stream_id)}
    if week:
        f["week"] = week
    if status:
        f["final_status"] = status
    if q:
        import re
        r = {"$regex": re.escape(q), "$options": "i"}
        names = [x["callup"] async for x in db.members.find({"stream_id": f["stream_id"], "full_name": r}, {"callup": 1})]
        f["$or"] = [{"callup": r}, {"callup": {"$in": names}}]
    total = await db.attendance.count_documents(f)
    rows = await db.attendance.find(f).sort("submitted_at", -1).skip((page - 1) * size).limit(size).to_list(size)
    return {"total": total, "items": ser(rows)}


@router.get("/{aid}/selfie")
async def selfie(aid: str, u=Depends(require_director)):
    a = await db.attendance.find_one({"_id": oid(aid)}, {"selfie_public_id": 1, "callup": 1})
    if not a or not a.get("selfie_public_id"):
        raise HTTPException(404, "Image not available (expired or none)")
    data = await storage.fetch(a["selfie_public_id"])
    await audit(u["sub"], "biometric.view_selfie", a["callup"])
    return StreamingResponse(io.BytesIO(data), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class OverrideIn(BaseModel):
    schedule_id: str
    callup: str
    new_status: str  # admin_marked_present | absent
    reason: str


@router.post("/override")
async def override(b: OverrideIn, u=Depends(require_director)):
    if b.new_status not in ("admin_marked_present", "absent"):
        raise HTTPException(400, "Override must be admin_marked_present or absent")
    if len(b.reason.strip()) < 5:
        raise HTTPException(422, "Please enter a reason with at least 5 characters")
    sch = await db.attendance_schedules.find_one({"_id": oid(b.schedule_id)})
    m = await db.members.find_one({"callup": b.callup.strip().upper()})
    if not sch or not m or m["stream_id"] != sch["stream_id"]:
        raise HTTPException(404, "Schedule/member mismatch")
    stream = await db.streams.find_one({"_id": sch["stream_id"]})
    if stream["status"] == "closed":
        raise HTTPException(409, "Stream is archived (read-only)")
    entry = {"by": u["sub"], "at": now(), "reason": b.reason.strip()}
    rec = await db.attendance.find_one({"schedule_id": sch["_id"], "callup": m["callup"]})
    if rec:
        entry |= {"from": rec["final_status"], "to": b.new_status}
        await db.attendance.update_one({"_id": rec["_id"]}, {"$set": {"final_status": b.new_status, "status_source": "administrative_override"}, "$push": {"overrides": entry}})
    else:
        entry |= {"from": "not_submitted", "to": b.new_status}
        try:
            await db.attendance.insert_one({"schedule_id": sch["_id"], "stream_id": sch["stream_id"], "callup": m["callup"], "week": sch["week"], "submitted_at": None,
                                            "face": None, "location": None, "final_status": b.new_status, "status_source": "administrative_override", "overrides": [entry]})
        except Exception:
            raise HTTPException(409, "Concurrent record exists; retry")
    await audit(u["sub"], "attendance.override", m["callup"], entry | {"at": entry["at"].isoformat()})
    await notify(m["callup"], m["stream_id"], "Attendance update", "Your attendance record was updated by the Camp Director.", "attendance")
    return {"ok": True}


@router.post("/schedules/{sid}/finalize")
async def finalize(sid: str, u=Depends(require_director)):
    """After the window closes: members with no record become 'absent' (policy-based, reviewable, not disciplinary)."""
    sch = await db.attendance_schedules.find_one({"_id": oid(sid)})
    if not sch or sch["closes_at"] > now():
        raise HTTPException(409, "Window has not closed yet")
    have = {r["callup"] async for r in db.attendance.find({"schedule_id": sch["_id"]}, {"callup": 1})}
    n = 0
    async for m in db.members.find({"stream_id": sch["stream_id"], "in_camp": True, "registration_status": {"$ne": "rejected"}}):
        if m["callup"] in have:
            continue
        try:
            await db.attendance.insert_one({"schedule_id": sch["_id"], "stream_id": sch["stream_id"], "callup": m["callup"], "week": sch["week"], "submitted_at": None,
                                            "face": None, "location": None, "final_status": "absent", "status_source": "policy", "overrides": []})
            n += 1
        except Exception:
            pass
    await db.attendance_schedules.update_one({"_id": sch["_id"]}, {"$set": {"finalized": True}})
    await audit(u["sub"], "attendance.finalize", str(sch["_id"]), {"absent": n})
    return {"marked_absent": n}
