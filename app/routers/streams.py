from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..db import db
from ..security import current_user, require_director
from ..services.audit import audit
from ..services.face import get_provider
from ..services import storage
from ..services.platoons import ensure_platoons
from ..util import now, oid, ser

router = APIRouter(prefix="/api", tags=["streams"])
STATUSES = {"upcoming", "active", "suspended", "closed"}


def label(year, batch, n):
    return f"{year} Batch {batch} Stream {n}"


class StreamIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    batch: str = Field(pattern=r"^[A-Za-z]{1,2}$")
    number: int = Field(ge=1, le=99)
    status: str = "upcoming"


@router.get("/public/streams")
async def registerable():
    """Unauthenticated: only active streams, minimal fields, for the QR registration page."""
    rows = await db.streams.find({"status": "active"}).to_list(50)
    return [{"id": str(s["_id"]), "label": s["label"]} for s in rows]


@router.get("/streams")
async def list_streams(u=Depends(current_user)):
    q = {} if u["role"] == "director" else {"status": {"$ne": "upcoming"}}
    return ser(await db.streams.find(q).sort([("year", -1), ("batch", -1), ("number", -1)]).to_list(500))


@router.post("/streams")
async def create_stream(b: StreamIn, u=Depends(require_director)):
    if b.status not in STATUSES - {"closed"}:
        raise HTTPException(400, "Invalid initial status")
    doc = {"year": b.year, "batch": b.batch.upper(), "number": b.number, "status": b.status,
           "label": label(b.year, b.batch.upper(), b.number), "created_at": now()}
    try:
        r = await db.streams.insert_one(doc)
    except Exception:
        raise HTTPException(409, "That batch/stream already exists")
    await ensure_platoons(r.inserted_id)
    await audit(u["sub"], "stream.create", doc["label"])
    return ser({**doc, "_id": r.inserted_id})


class StatusIn(BaseModel):
    status: str


@router.patch("/streams/{sid}/status")
async def set_status(sid: str, b: StatusIn, u=Depends(require_director)):
    if b.status not in STATUSES:
        raise HTTPException(400, "Invalid status")
    s = await db.streams.find_one({"_id": oid(sid)})
    if not s:
        raise HTTPException(404, "Stream not found")
    if s["status"] == "closed":
        raise HTTPException(409, "Closed streams are archived and read-only")
    await db.streams.update_one({"_id": s["_id"]}, {"$set": {"status": b.status, "status_changed_at": now()}})
    review = None
    if b.status == "closed":
        # Controlled review: nothing is auto-cleared. Items are queued for the Director to resolve.
        await db.closure_review.delete_many({"stream_id": s["_id"], "resolved": False})
        items = []
        async for a in db.assignments.find({"stream_id": s["_id"], "status": "active"}):
            items.append({"stream_id": s["_id"], "type": "bed_assignment", "callup": a["callup"], "ref": a["bed_code"], "resolved": False, "created_at": now()})
        async for e in db.exits.find({"stream_id": s["_id"], "status": {"$in": ["draft", "submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision", "more_info_required", "approved", "departing", "departed", "overdue", "returned"]}}):
            items.append({"stream_id": s["_id"], "type": "exit_record", "callup": e["callup"], "ref": str(e["_id"]), "resolved": False, "created_at": now()})
        if items:
            await db.closure_review.insert_many(items)
        review = len(items)
    await audit(u["sub"], "stream.status", s["label"], {"to": b.status, "review_items": review})
    return {"ok": True, "closure_review_items": review}


@router.get("/streams/{sid}/closure-review")
async def closure_review(sid: str, u=Depends(require_director)):
    return ser(await db.closure_review.find({"stream_id": oid(sid)}).to_list(5000))


@router.post("/closure-review/{iid}/resolve")
async def resolve_item(iid: str, u=Depends(require_director)):
    r = await db.closure_review.update_one({"_id": oid(iid)}, {"$set": {"resolved": True, "resolved_by": u["sub"], "resolved_at": now()}})
    if not r.matched_count:
        raise HTTPException(404, "Not found")
    await audit(u["sub"], "closure.resolve", iid)
    return {"ok": True}


# ---- camp config (geofence etc.) ----
class CampConfig(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0, le=20000)
    max_accuracy_m: float = Field(gt=0, le=2000)


async def get_camp_config() -> dict:
    c = await db.config.find_one({"_id": "camp"})
    if c:
        cfg = c
    else:
        cfg = {"lat": settings.camp_lat, "lon": settings.camp_lon, "radius_m": settings.camp_radius_m, "max_accuracy_m": settings.max_gps_accuracy_m}
    cfg["configured"] = not (cfg["lat"] == 0 and cfg["lon"] == 0)
    return cfg


@router.get("/config/camp")
async def read_cfg(u=Depends(require_director)):
    c = await get_camp_config()
    return {k: c[k] for k in ("lat", "lon", "radius_m", "max_accuracy_m", "configured")}


@router.put("/config/camp")
async def write_cfg(b: CampConfig, u=Depends(require_director)):
    values = b.model_dump()
    values["configured"] = not (b.lat == 0 and b.lon == 0)
    await db.config.update_one({"_id": "camp"}, {"$set": values}, upsert=True)
    await audit(u["sub"], "config.camp", "geofence", b.model_dump())
    return {"ok": True}


@router.get("/config/face")
async def read_face_config(u=Depends(require_director)):
    provider = get_provider()
    enabled = bool(getattr(provider, "available", False))
    provider_name = settings.face_provider
    return {
        "enabled": enabled,
        "provider": provider_name,
        "liveness_mode": settings.liveness_mode if provider_name == "rekognition" else "none",
        "message": (
            "Automatic face matching is ready. Unclear results still go to manual review."
            if enabled else
            "Automatic face matching is not ready. Attendance will still be received, but the Camp Director must review each face manually."
        ),
        "setup_hint": None if enabled else getattr(provider, "unavailable_reason", "Face matching is disabled in the system settings."),
    }


async def _purge(stream_id):
    async for m in db.members.find({"stream_id": stream_id, "enrollment": {"$exists": True}}):
        for k in ("front", "side"):
            try:
                await storage.delete(m["enrollment"][k])
            except Exception:
                pass
        await db.members.update_one({"_id": m["_id"]}, {"$unset": {"enrollment": ""}, "$set": {"biometric_status": "purged", "biometric_purged_at": now()}})
    async for a in db.attendance.find({"stream_id": stream_id, "selfie_public_id": {"$exists": True}}):
        try:
            await storage.delete(a["selfie_public_id"])
        except Exception:
            pass
        await db.attendance.update_one({"_id": a["_id"]}, {"$unset": {"selfie_public_id": ""}, "$set": {"selfie_purged_at": now()}})


@router.post("/streams/{sid}/purge-biometrics")
async def purge_biometrics(sid: str, bg: BackgroundTasks, u=Depends(require_director)):
    """Retention step for closed streams: permanently deletes enrolment photos and attendance selfies.
    Attendance results, scores and the audit trail are kept."""
    s = await db.streams.find_one({"_id": oid(sid)})
    if not s or s["status"] != "closed":
        raise HTTPException(409, "Biometric purge is only available for closed streams")
    if await db.closure_review.count_documents({"stream_id": s["_id"], "resolved": False}):
        raise HTTPException(409, "Resolve the closure review first")
    bg.add_task(_purge, s["_id"])
    await audit(u["sub"], "biometrics.purge", s["label"])
    return {"ok": True, "message": "Purge started"}
