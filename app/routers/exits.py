"""Configurable, evidence-aware camp exit workflow."""
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from ..db import db
from ..security import active_stream, require_director, require_member, require_platoon_officer
from ..services import storage
from ..services.audit import audit
from ..services.notify import DIRECTOR, notify
from ..util import aware, now, oid, ser
from .hostels import auto_assign_bed, release_bed

router = APIRouter(prefix="/api/exits", tags=["exits"])
OPEN = ["draft", "submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision",
        "more_info_required", "approved", "departing", "departed", "overdue"]
SAFE_FORMATS = {"pdf": ("application/pdf", "pdf"), "jpg": ("image/jpeg", "jpg"), "jpeg": ("image/jpeg", "jpg"),
                "png": ("image/png", "png"), "webp": ("image/webp", "webp")}


class DocumentType(BaseModel):
    id: str | None = None
    name: str = Field(min_length=2, max_length=100)
    purpose: str = Field(min_length=3, max_length=500)
    classification: str = "supporting_evidence"  # identity | supporting_evidence | reason_evidence
    required: bool = False
    active: bool = True
    allowed_formats: list[str] = ["pdf", "jpg", "png"]
    max_size_mb: int = Field(default=5, ge=1, le=20)


class CategoryIn(BaseModel):
    stream_id: str
    name: str = Field(min_length=2, max_length=100)
    instructions: str = Field(default="", max_length=2000)
    active: bool = True
    duration_options: list[str] = ["temporary", "remaining_period"]
    emergency_medical: bool = False
    member_can_cancel: bool = True
    allow_concurrent_requests: bool = False
    require_platoon_confirmation: bool = True
    require_document_review: bool = True
    bed_release_rule: str = "after_effective_date"  # retain | after_approved_departure | after_effective_date
    document_types: list[DocumentType] = []
    required_document_groups: list[list[str]] = []  # each group requires one accepted member


def _requirements(category):
    return {"version": category["version"], "name": category["name"], "instructions": category.get("instructions", ""),
            "duration_options": category.get("duration_options", []), "emergency_medical": category.get("emergency_medical", False),
            "require_platoon_confirmation": category.get("require_platoon_confirmation", True),
            "require_document_review": category.get("require_document_review", True),
            "allow_concurrent_requests": category.get("allow_concurrent_requests", False),
            "bed_release_rule": category.get("bed_release_rule", "after_effective_date"),
            "document_types": category.get("document_types", []),
            "required_document_groups": category.get("required_document_groups", [])}


def _public_exit(e, include_files=False):
    d = dict(e)
    d.pop("request_lock_id", None)
    if not include_files:
        d["documents"] = [{k: v for k, v in x.items() if k != "file_ref"} for x in d.get("documents", [])]
    return ser(d)


async def _category_for_request(e):
    return await db.exit_categories.find_one({"_id": e.get("category_id"), "stream_id": e["stream_id"]})


async def _assert_stream_mutable(e):
    stream = await db.streams.find_one({"_id": e["stream_id"]}, {"status": 1})
    if not stream or stream.get("status") == "closed":
        raise HTTPException(409, "This stream is closed and its exit records are read-only")


def _latest_docs(e):
    latest = {}
    for doc in sorted(e.get("documents", []), key=lambda x: x.get("uploaded_at", now())):
        latest[doc["document_type_id"]] = doc
    return latest


def _extra_satisfied(latest, extra, accepted=False):
    requested_at = extra.get("requested_at")
    for doc_id in extra.get("document_type_ids", []):
        document = latest.get(doc_id)
        if not document:
            continue
        if requested_at and aware(document.get("uploaded_at", datetime.min.replace(tzinfo=requested_at.tzinfo))) <= requested_at:
            continue
        if accepted and document.get("review_status") != "accepted":
            continue
        return True
    return False


def _all_requirements_accepted(e):
    req = e.get("requirements_snapshot") or {}
    latest = _latest_docs(e)
    reviewed = req.get("require_document_review", True)
    for dt in req.get("document_types", []):
        if dt.get("active", True) and dt.get("required"):
            doc = latest.get(dt["id"])
            if not doc or (reviewed and doc["review_status"] != "accepted"):
                return False
    for group in req.get("required_document_groups", []):
        if not any((latest.get(doc_id) and (not reviewed or latest[doc_id].get("review_status") == "accepted")) for doc_id in group):
            return False
    for extra in req.get("additional_requirements", []):
        if not _extra_satisfied(latest, extra, accepted=True):
            return False
    if reviewed:
        required_ids = {d["id"] for d in req.get("document_types", []) if d.get("required") and d.get("active", True)}
        required_ids.update(doc_id for group in req.get("required_document_groups", []) for doc_id in group)
        required_ids.update(doc_id for extra in req.get("additional_requirements", []) for doc_id in extra["document_type_ids"])
        for doc_id in required_ids:
            doc = latest.get(doc_id)
            if not doc or doc["review_status"] != "accepted":
                return False
    return True


def _uploaded_requirements(e):
    req = e.get("requirements_snapshot") or {}
    latest = _latest_docs(e)
    missing = []
    for dt in req.get("document_types", []):
        if not dt.get("required") or not dt.get("active", True):
            continue
        current = latest.get(dt["id"])
        if not current:
            missing.append(dt["name"] + " (missing)")
        elif current.get("review_status") != "accepted":
            missing.append(dt["name"] + " (" + current.get("review_status", "pending review").replace("_", " ") + ")")
    for i, group in enumerate(req.get("required_document_groups", []), start=1):
        if not any(latest.get(doc_id, {}).get("review_status") == "accepted" for doc_id in group):
            labels = [d["name"] for d in req.get("document_types", []) if d["id"] in group]
            missing.append("one of: " + ", ".join(labels or [f"required document group {i}"]))
    for extra in req.get("additional_requirements", []):
        if not _extra_satisfied(latest, extra, accepted=True):
            missing.append(extra["reason"])
    return missing


async def _advance(eid):
    e = await db.exits.find_one({"_id": eid})
    if not e or e.get("status") in ("rejected", "cancelled", "approved", "departed", "overdue", "returned", "completed"):
        return
    req = e.get("requirements_snapshot") or {}
    target = None
    latest = _latest_docs(e)
    if any(not _extra_satisfied(latest, extra, accepted=True) for extra in req.get("additional_requirements", [])):
        target = "more_info_required"
    if target is None:
        if req.get("require_platoon_confirmation", True) and e.get("platoon_confirmation", {}).get("status") != "confirmed":
            target = "awaiting_platoon_confirmation"
        elif not _all_requirements_accepted(e):
            target = "awaiting_document_review"
        else:
            target = "awaiting_director_decision"
    await db.exits.update_one({"_id": eid}, {"$set": {"status": target, "updated_at": now()}})


@router.get("/categories")
async def list_categories(stream_id: str, m=Depends(require_member)):
    sid = oid(stream_id)
    if sid != m["stream_id"]:
        raise HTTPException(403, "Categories are only available for your stream")
    stream = await db.streams.find_one({"_id": sid}, {"status": 1})
    if not stream or stream.get("status") != "active":
        raise HTTPException(409, "Exit requests are unavailable because this camp stream is not active. Contact the Camp Director.")
    rows = await db.exit_categories.find({"stream_id": sid, "active": True}).sort("name", 1).to_list(100)
    return ser(rows)


@router.get("/categories/admin")
async def list_categories_admin(stream_id: str, u=Depends(require_director)):
    return ser(await db.exit_categories.find({"stream_id": oid(stream_id)}).sort("name", 1).to_list(100))


def _clean_category(b: CategoryIn, old=None):
    if b.bed_release_rule not in ("retain", "after_approved_departure", "after_effective_date"):
        raise HTTPException(422, "Choose a valid bed-release rule")
    if not b.duration_options or any(x not in ("temporary", "remaining_period") for x in b.duration_options):
        raise HTTPException(422, "Choose at least one valid exit duration")
    if "temporary" in b.duration_options and "remaining_period" not in b.duration_options:
        pass
    docs, ids = [], set()
    for doc in b.document_types:
        doc_id = doc.id or uuid4().hex
        if doc_id in ids:
            raise HTTPException(422, "Document type identifiers must be unique")
        ids.add(doc_id)
        if doc.classification not in ("identity", "supporting_evidence", "reason_evidence"):
            raise HTTPException(422, "Choose an allowed document purpose")
        formats = [x.lower().lstrip(".") for x in doc.allowed_formats]
        if not formats or any(x not in SAFE_FORMATS for x in formats):
            raise HTTPException(422, "Supported formats are PDF, JPG, PNG, and WEBP")
        docs.append({**doc.model_dump(), "id": doc_id, "allowed_formats": formats})
    for group in b.required_document_groups:
        if not group or any(x not in ids for x in group):
            raise HTTPException(422, "Every alternative document group must reference configured document types")
    return {"name": b.name.strip(), "instructions": b.instructions.strip(), "active": b.active,
            "duration_options": list(dict.fromkeys(b.duration_options)), "emergency_medical": b.emergency_medical,
            "member_can_cancel": b.member_can_cancel, "require_platoon_confirmation": b.require_platoon_confirmation,
            "allow_concurrent_requests": b.allow_concurrent_requests,
            "require_document_review": b.require_document_review, "bed_release_rule": b.bed_release_rule,
            "document_types": docs, "required_document_groups": b.required_document_groups,
            "version": (old or {}).get("version", 0) + 1}


@router.post("/categories")
async def create_category(b: CategoryIn, u=Depends(require_director)):
    sid = oid(b.stream_id)
    await active_stream(sid)
    doc = _clean_category(b)
    doc.update({"stream_id": sid, "created_at": now(), "created_by": u["sub"]})
    try:
        result = await db.exit_categories.insert_one(doc)
    except Exception:
        raise HTTPException(409, "An exit category with this name already exists in the stream")
    await audit(u["sub"], "exit.category.create", doc["name"], {"version": 1})
    return ser({**doc, "_id": result.inserted_id})


@router.put("/categories/{cid}")
async def update_category(cid: str, b: CategoryIn, u=Depends(require_director)):
    sid = oid(b.stream_id)
    await active_stream(sid)
    old = await db.exit_categories.find_one({"_id": oid(cid), "stream_id": sid})
    if not old:
        raise HTTPException(404, "Exit category not found")
    values = _clean_category(b, old)
    await db.exit_categories.update_one({"_id": old["_id"]}, {"$set": {**values, "updated_at": now(), "updated_by": u["sub"]}})
    await audit(u["sub"], "exit.category.update", old["name"], {"version": values["version"], "active": values["active"]})
    return {"ok": True, "version": values["version"]}


class OfficerConfirmation(BaseModel):
    confirmed: bool
    notes: str = Field(default="", max_length=1000)


class ExitIn(BaseModel):
    category_id: str
    duration: str  # temporary | remaining_period
    reason: str = Field(min_length=5, max_length=2000)
    depart_at: datetime
    return_at: datetime | None = None
    urgent: bool = False


@router.post("")
async def create_draft(b: ExitIn, m=Depends(require_member)):
    stream = await active_stream(m["stream_id"])
    category = await db.exit_categories.find_one({"_id": oid(b.category_id), "stream_id": stream["_id"], "active": True})
    if not category:
        raise HTTPException(422, "Choose an active exit category")
    if b.duration not in category.get("duration_options", []):
        raise HTTPException(422, "This category does not allow the selected exit duration")
    departure, ret = aware(b.depart_at), aware(b.return_at) if b.return_at else None
    if departure <= now() and not (b.urgent and category.get("emergency_medical")):
        raise HTTPException(422, "Departure must be in the future")
    if b.duration == "temporary" and (not ret or ret <= departure):
        raise HTTPException(422, "A temporary exit needs an expected return after departure")
    if b.duration == "remaining_period":
        ret = None
    if b.urgent and not category.get("emergency_medical"):
        raise HTTPException(422, "Urgent medical processing is only available for a category configured by the Camp Director")
    existing = await db.exits.find_one({"stream_id": stream["_id"], "callup": m["callup"], "status": {"$in": OPEN}})
    if existing and not (category.get("allow_concurrent_requests") and existing.get("allow_concurrent_requests")):
        raise HTTPException(409, "You already have an open exit request")
    lock = None
    if not category.get("allow_concurrent_requests"):
        # A returned record is terminal. Clear locks left by older versions
        # that required a separate Director completion click after return.
        await db.exit_request_locks.delete_one({"stream_id": stream["_id"], "callup": m["callup"]})
        try:
            lock = await db.exit_request_locks.insert_one({"stream_id": stream["_id"], "callup": m["callup"], "created_at": now()})
        except Exception:
            raise HTTPException(409, "You already have an open exit request")
    doc = {"stream_id": stream["_id"], "callup": m["callup"], "full_name": m["full_name"], "category_id": category["_id"],
           "category_name": category["name"], "category_version_at_draft": category["version"], "duration": b.duration,
           "type": b.duration, "reason": b.reason.strip(), "depart_at": departure, "return_at": ret, "urgent": b.urgent,
           "status": "draft", "created_at": now(), "updated_at": now(), "documents": [], "log": [], "decisions": [],
           "member_can_cancel": category.get("member_can_cancel", True),
           "allow_concurrent_requests": category.get("allow_concurrent_requests", False)}
    try:
        r = await db.exits.insert_one(doc)
    except Exception:
        if lock:
            await db.exit_request_locks.delete_one({"_id": lock.inserted_id})
        raise
    if lock:
        doc["request_lock_id"] = lock.inserted_id
        await db.exits.update_one({"_id": r.inserted_id}, {"$set": {"request_lock_id": lock.inserted_id}})
    doc["_id"] = r.inserted_id
    await audit(m["callup"], "exit.draft.create", str(r.inserted_id), {"category": category["name"], "duration": b.duration})
    return _public_exit(doc)


@router.get("/mine")
async def mine(m=Depends(require_member)):
    rows = await db.exits.find({"stream_id": m["stream_id"], "callup": m["callup"]}).sort("created_at", -1).to_list(100)
    return [_public_exit(x) for x in rows]


@router.get("/officer")
async def officer_requests(u=Depends(require_platoon_officer)):
    rows = await db.exits.find({"status": "awaiting_platoon_confirmation", "platoon_assignment_snapshot.officer_username": u["sub"],
                                "platoon_assignment_snapshot.platoon_id": u["platoon_id"]}).sort("created_at", 1).to_list(200)
    # Officers see member identity and the request reason, never evidence references or private files.
    return [{"id": str(e["_id"]), "callup": e["callup"], "full_name": e["full_name"], "category_name": e["category_name"],
             "duration": e["duration"], "requested_departure": e["depart_at"].isoformat(), "reason": e["reason"],
             "confirmation": e.get("platoon_confirmation")} for e in rows]


@router.post("/officer/{eid}/confirm")
async def officer_confirm(eid: str, b: "OfficerConfirmation", u=Depends(require_platoon_officer)):
    e = await db.exits.find_one({"_id": oid(eid), "status": "awaiting_platoon_confirmation",
                                 "platoon_assignment_snapshot.officer_username": u["sub"],
                                 "platoon_assignment_snapshot.platoon_id": u["platoon_id"]})
    if not e:
        raise HTTPException(404, "This request is not assigned to your platoon")
    await _assert_stream_mutable(e)
    entry = {"status": "confirmed" if b.confirmed else "declined", "officer_username": u["sub"],
             "platoon_id": u["platoon_id"], "platoon_number": e["platoon_assignment_snapshot"]["platoon_number"],
             "at": now(), "notes": b.notes.strip()}
    await db.exits.update_one({"_id": e["_id"], "status": "awaiting_platoon_confirmation"}, {"$set": {"platoon_confirmation": entry}, "$push": {"platoon_confirmation_history": entry}})
    await audit(u["sub"], "exit.platoon_confirmation", str(e["_id"]), {"confirmed": b.confirmed, "notes": b.notes.strip()})
    if b.confirmed:
        await _advance(e["_id"])
        await notify(DIRECTOR, e["stream_id"], "Exit request ready for review", "A platoon officer confirmed an exit request.", "exit_review")
    else:
        await db.exits.update_one({"_id": e["_id"]}, {"$set": {"status": "more_info_required", "platoon_confirmation": entry}})
        await notify(e["callup"], e["stream_id"], "Exit request needs attention", "Your exit request needs an update. Open the app to review it.", "exit_action_required")
        await notify(DIRECTOR, e["stream_id"], "Exit confirmation declined", "A platoon officer needs the Camp Director to review an exit request.", "exit_review")
    return {"ok": True}


@router.post("/{eid}/submit")
async def submit_request(eid: str, m=Depends(require_member)):
    e = await db.exits.find_one({"_id": oid(eid), "stream_id": m["stream_id"], "callup": m["callup"], "status": {"$in": ["draft", "more_info_required"]}})
    if not e:
        raise HTTPException(404, "Draft or request needing updates not found")
    await _assert_stream_mutable(e)
    category = await _category_for_request(e)
    old_snapshot = e.get("requirements_snapshot")
    if not category or (not category.get("active") and not old_snapshot):
        raise HTTPException(409, "This exit category is no longer available; contact the Camp Director")
    snapshot = old_snapshot or _requirements(category)
    if e["duration"] not in snapshot.get("duration_options", []):
        raise HTTPException(409, "The category's allowed durations changed. Contact the Camp Director before resubmitting")
    latest = _latest_docs(e)
    missing = []
    for dt in snapshot["document_types"]:
        if dt.get("active", True) and dt.get("required") and dt["id"] not in latest:
            missing.append(dt["name"])
    for i, group in enumerate(snapshot["required_document_groups"], 1):
        if not any(did in latest for did in group):
            labels = [d["name"] for d in snapshot["document_types"] if d["id"] in group]
            missing.append("one of: " + ", ".join(labels or [f"document group {i}"]))
    for extra in snapshot.get("additional_requirements", []):
        if not _extra_satisfied(latest, extra):
            missing.append(extra["reason"])
    emergency = bool(e.get("urgent") and snapshot.get("emergency_medical"))
    if missing and not emergency:
        raise HTTPException(422, "Upload the required evidence before submitting: " + "; ".join(missing))
    previous_confirmation = e.get("platoon_confirmation") or {}
    preserve_confirmation = bool(old_snapshot and previous_confirmation.get("status") == "confirmed")
    platoon_snapshot = e.get("platoon_assignment_snapshot") if preserve_confirmation else None
    if snapshot.get("require_platoon_confirmation") and not emergency and not preserve_confirmation:
        assignment = await db.platoon_assignments.find_one({"stream_id": e["stream_id"], "callup": e["callup"], "status": "active"})
        if assignment:
            platoon = await db.platoons.find_one({"_id": assignment["platoon_id"]})
            platoon_snapshot = {"assignment_id": assignment["_id"], "platoon_id": assignment["platoon_id"],
                                "platoon_number": assignment["platoon_number"], "officer_username": platoon.get("officer_username") if platoon else None,
                                "assignment_at": assignment["assigned_at"]}
        else:
            platoon_snapshot = {"assignment_id": None, "platoon_id": None, "platoon_number": None, "officer_username": None,
                                "assignment_at": None, "exception": "Member has no confirmed platoon assignment"}
    candidate = {**e, "requirements_snapshot": snapshot}
    confirmation = (previous_confirmation if preserve_confirmation else
                    {"status": "not_required_emergency" if emergency else "awaiting_officer", "notes": ""}
                    if snapshot.get("require_platoon_confirmation") else {"status": "not_required"})
    needs_confirmation = snapshot.get("require_platoon_confirmation") and not emergency and confirmation.get("status") != "confirmed"
    status = ("awaiting_director_decision" if emergency else
              "awaiting_platoon_confirmation" if needs_confirmation else
              "awaiting_director_decision" if _all_requirements_accepted(candidate) else "awaiting_document_review")
    await db.exits.update_one({"_id": e["_id"], "status": {"$in": ["draft", "more_info_required"]}}, {"$set": {
        "requirements_snapshot": snapshot, "submitted_at": now(), "status": status, "platoon_assignment_snapshot": platoon_snapshot,
        "platoon_confirmation": confirmation,
        "updated_at": now(),
    }, "$push": {"log": {"event": "submitted", "by": m["callup"], "at": now()},
                "submission_history": {"at": now(), "requirements_version": snapshot["version"], "platoon_assignment": platoon_snapshot}}})
    await audit(m["callup"], "exit.submit", str(e["_id"]), {"requirements_version": snapshot["version"], "status": status})
    await notify(DIRECTOR, e["stream_id"], "Exit request submitted", "A corps member submitted an exit request for review.", "exit")
    if e.get("urgent"):
        await notify(DIRECTOR, e["stream_id"], "Urgent medical exit request", "An urgent medical exit request needs immediate attention.", "urgent_exit")
    officer = (platoon_snapshot or {}).get("officer_username")
    if officer and needs_confirmation:
        await notify(officer, e["stream_id"], "Platoon confirmation required", "An exit request needs your member and request confirmation.", "exit_confirmation")
    elif needs_confirmation:
        await notify(DIRECTOR, e["stream_id"], "Exit request needs platoon assignment", "An exit request cannot be routed because the member has no confirmed Platoon Officer.", "exit_exception")
    return {"status": status}


@router.post("/{eid}/documents")
async def upload_document(eid: str, document_type_id: str = Form(...), file: UploadFile = File(...), m=Depends(require_member)):
    e = await db.exits.find_one({"_id": oid(eid), "stream_id": m["stream_id"], "callup": m["callup"],
                                 "status": {"$in": ["draft", "awaiting_document_review", "more_info_required"]}})
    if not e:
        raise HTTPException(409, "Documents can only be added to a draft or a request that needs more evidence")
    await _assert_stream_mutable(e)
    category = await _category_for_request(e)
    snapshot = e.get("requirements_snapshot") or (_requirements(category) if category else {})
    doc_type = next((d for d in snapshot.get("document_types", []) if d["id"] == document_type_id and d.get("active", True)), None)
    if not doc_type:
        raise HTTPException(422, "Choose a document type configured for this exit category")
    previous = _latest_docs(e).get(document_type_id)
    if previous and previous.get("review_status") not in ("rejected", "replacement_required") and e["status"] != "draft":
        requested = any(document_type_id in x["document_type_ids"] for x in snapshot.get("additional_requirements", []))
        if not requested:
            raise HTTPException(409, "A replacement is only allowed when the Camp Director requested one")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in doc_type.get("allowed_formats", []):
        raise HTTPException(415, "This file type is not accepted for the selected document")
    expected_mime = SAFE_FORMATS[ext][0]
    if file.content_type != expected_mime:
        raise HTTPException(415, "The file content does not match its file type")
    max_bytes = int(doc_type.get("max_size_mb", 5)) * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if not data or len(data) > max_bytes:
        raise HTTPException(413, f"File must be smaller than {doc_type.get('max_size_mb', 5)} MB")
    signatures = {"pdf": data.startswith(b"%PDF-"), "jpg": data.startswith(b"\xff\xd8\xff"),
                  "jpeg": data.startswith(b"\xff\xd8\xff"), "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                  "webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"}
    if not signatures.get(ext, False):
        raise HTTPException(415, "The file contents do not match the selected file type")
    if not storage.configured():
        raise HTTPException(503, "Private document storage is not configured")
    try:
        file_ref = await storage.upload_private_document(data, file.filename or f"evidence.{ext}", f"nysc/exits/{e['stream_id']}/{e['_id']}")
    except Exception:
        raise HTTPException(503, "Could not store this document securely. Please try again.")
    item = {"id": uuid4().hex, "document_type_id": document_type_id, "document_type_name": doc_type["name"],
            "classification": doc_type.get("classification", "supporting_evidence"), "file_ref": file_ref,
            "filename": file.filename, "mime_type": file.content_type, "size_bytes": len(data), "uploaded_at": now(),
            "uploaded_by": m["callup"], "review_status": "pending_review", "reviewer": None, "reviewed_at": None,
            "review_notes": "", "replaces_document_id": previous["id"] if previous else None, "review_history": []}
    await db.exits.update_one({"_id": e["_id"]}, {"$push": {"documents": item, "log": {"event": "document_uploaded", "by": m["callup"], "at": now(), "document_type": doc_type["name"]}}, "$set": {"updated_at": now()}})
    await audit(m["callup"], "exit.document.upload", str(e["_id"]), {"document_id": item["id"], "document_type": doc_type["name"], "replaces": item["replaces_document_id"]})
    if e["status"] != "draft":
        await notify(DIRECTOR, e["stream_id"], "Exit evidence uploaded", "New evidence is ready for review.", "exit_document")
    return {"id": item["id"], "document_type": item["document_type_name"], "review_status": item["review_status"]}


class DocumentReview(BaseModel):
    decision: str  # accepted | rejected | replacement_required
    notes: str = Field(default="", max_length=1000)


@router.post("/{eid}/documents/{did}/review")
async def review_document(eid: str, did: str, b: DocumentReview, u=Depends(require_director)):
    if b.decision not in ("accepted", "rejected", "replacement_required"):
        raise HTTPException(422, "Choose accepted, rejected, or replacement required")
    if b.decision != "accepted" and len(b.notes.strip()) < 5:
        raise HTTPException(422, "Enter at least 5 characters explaining the document decision")
    e = await db.exits.find_one({"_id": oid(eid)})
    if not e or not any(d["id"] == did for d in e.get("documents", [])):
        raise HTTPException(404, "Document not found")
    if e.get("status") not in ("submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision", "more_info_required"):
        raise HTTPException(409, "Documents can no longer be reviewed in this request")
    await _assert_stream_mutable(e)
    docs = []
    for d in e["documents"]:
        if d["id"] == did:
            d = {**d, "review_status": b.decision, "reviewer": u["sub"], "reviewed_at": now(),
                 "review_notes": b.notes.strip(), "review_history": d.get("review_history", []) + [{"status": b.decision, "reviewer": u["sub"], "at": now(), "notes": b.notes.strip()}]}
        docs.append(d)
    await db.exits.update_one({"_id": e["_id"]}, {"$set": {"documents": docs, "updated_at": now()},
        "$push": {"log": {"event": "document_reviewed", "by": u["sub"], "at": now(), "document_id": did, "decision": b.decision, "reason": b.notes.strip()}}})
    await audit(u["sub"], "exit.document.review", str(e["_id"]), {"document_id": did, "decision": b.decision, "reason": b.notes.strip()})
    if b.decision != "accepted":
        await db.exits.update_one({"_id": e["_id"]}, {"$set": {"status": "more_info_required"}})
        await notify(e["callup"], e["stream_id"], "Exit evidence needs an update", "A document needs attention. Open your exit request to see the Camp Director's note.", "exit_action_required")
    else:
        await _advance(e["_id"])
    return {"ok": True}


class RequestInfo(BaseModel):
    document_type_ids: list[str] = []
    reason: str = Field(min_length=5, max_length=1000)


@router.post("/{eid}/request-info")
async def request_more_info(eid: str, b: RequestInfo, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid)})
    if not e or e["status"] not in ("submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision", "more_info_required"):
        raise HTTPException(409, "This request cannot be updated")
    await _assert_stream_mutable(e)
    req = e.get("requirements_snapshot") or {}
    valid_ids = {d["id"] for d in req.get("document_types", []) if d.get("active", True)}
    if not b.document_type_ids or any(x not in valid_ids for x in b.document_type_ids):
        raise HTTPException(422, "Select one or more active document types configured for this category")
    req.setdefault("additional_requirements", []).append({"document_type_ids": list(dict.fromkeys(b.document_type_ids)),
        "reason": b.reason.strip(), "requested_by": u["sub"], "requested_at": now()})
    await db.exits.update_one({"_id": e["_id"]}, {"$set": {"requirements_snapshot": req, "status": "more_info_required", "updated_at": now()},
        "$push": {"log": {"event": "additional_evidence_requested", "by": u["sub"], "at": now(), "document_types": b.document_type_ids, "reason": b.reason.strip()}}})
    await audit(u["sub"], "exit.request_info", str(e["_id"]), {"document_types": b.document_type_ids, "reason": b.reason.strip()})
    await notify(e["callup"], e["stream_id"], "More information needed", "The Camp Director needs more information for your exit request. Open the app to view the requirements.", "exit_action_required")
    return {"ok": True}


@router.get("")
async def list_exits(stream_id: str, status: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, le=200), u=Depends(require_director)):
    sid = oid(stream_id)
    await mark_overdue(sid)
    f = {"stream_id": sid}
    if status:
        f["status"] = status
    total = await db.exits.count_documents(f)
    rows = await db.exits.find(f).sort("created_at", -1).skip((page - 1) * size).limit(size).to_list(size)
    return {"total": total, "items": [_public_exit(e) for e in rows]}


@router.get("/{eid}")
async def exit_detail(eid: str, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid)})
    if not e:
        raise HTTPException(404, "Exit request not found")
    return _public_exit(e)


@router.get("/{eid}/documents/{did}/file")
async def document_file(eid: str, did: str, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid)})
    d = next((x for x in e.get("documents", []) if x["id"] == did), None) if e else None
    if not d:
        raise HTTPException(404, "Document not found")
    await audit(u["sub"], "exit.document.view", str(e["_id"]), {"document_id": did, "callup": e["callup"]})
    data = await storage.fetch_private_document(d["file_ref"], d["mime_type"])
    from fastapi.responses import Response
    return Response(data, media_type=d["mime_type"], headers={"Cache-Control": "no-store", "Content-Disposition": "inline"})


class Decision(BaseModel):
    approve: bool
    reason: str = Field(min_length=5, max_length=1000)
    effective_date: datetime | None = None
    emergency: bool = False


@router.post("/{eid}/decide")
async def decide(eid: str, b: Decision, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid)})
    decision_stages = {"submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision", "more_info_required"}
    if not e or e["status"] not in decision_stages:
        raise HTTPException(409, "This request is not ready for a Camp Director decision")
    await _assert_stream_mutable(e)
    if b.approve and e["status"] != "awaiting_director_decision" and not (b.emergency and e.get("urgent")):
        raise HTTPException(409, "Approval is available after the required reviews are complete")
    category = await _category_for_request(e)
    req = e.get("requirements_snapshot") or (_requirements(category) if category else {})
    if not category and not req:
        raise HTTPException(409, "Exit category is no longer available; review the request history")
    emergency = b.emergency and e.get("urgent") and req.get("emergency_medical", False)
    if b.emergency and not emergency:
        raise HTTPException(422, "This request is not eligible for the configured emergency medical pathway")
    if b.approve and not emergency:
        if req.get("require_platoon_confirmation", True) and e.get("platoon_confirmation", {}).get("status") != "confirmed":
            raise HTTPException(409, "Wait for Platoon Officer confirmation before approval")
        if not _all_requirements_accepted(e):
            raise HTTPException(409, "Required documents must be reviewed and accepted before approval")
    if b.approve and e["duration"] == "remaining_period":
        rule = req.get("bed_release_rule", "after_effective_date")
        if rule == "after_effective_date" and not b.effective_date:
            raise HTTPException(422, "Set the effective bed-release date for this permanent exit")
        if b.effective_date and aware(b.effective_date) < e["depart_at"]:
            raise HTTPException(422, "The effective date cannot be before the requested departure")
    entry = {"decision": "approved" if b.approve else "rejected", "by": u["sub"], "at": now(), "reason": b.reason.strip(), "emergency": emergency,
             "requirements_version": req.get("version")}
    update = {"status": "approved" if b.approve else "rejected", "decided_by": u["sub"], "decided_at": entry["at"],
              "decision_note": b.reason.strip(), "updated_at": now()}
    if b.approve and e["duration"] == "remaining_period":
        update["effective_date"] = aware(b.effective_date) if b.effective_date else e["depart_at"]
    if emergency:
        update["emergency_decision"] = {**entry, "outstanding_documents": _uploaded_requirements(e),
                                        "platoon_confirmation_outstanding": req.get("require_platoon_confirmation", True) and e.get("platoon_confirmation", {}).get("status") != "confirmed"}
    changed = await db.exits.update_one({"_id": e["_id"], "status": e["status"]},
                                        {"$set": update, "$push": {"decisions": entry, "log": {"event": "decision", **entry}}})
    if not changed.modified_count:
        raise HTTPException(409, "This exit request was already decided")
    await audit(u["sub"], "exit.decide", str(e["_id"]), entry)
    if not b.approve:
        await db.exit_request_locks.delete_one({"stream_id": e["stream_id"], "callup": e["callup"]})
    await notify(e["callup"], e["stream_id"], "Exit request decision", "The Camp Director has made a decision on your exit request. Open the app to view it.", "exit_decision")
    return {"status": update["status"]}


class Mark(BaseModel):
    at: datetime | None = None
    note: str = Field(default="", max_length=1000)


@router.post("/{eid}/depart")
async def depart(eid: str, b: Mark, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid), "status": "approved"})
    if not e:
        raise HTTPException(409, "Only approved requests can be marked departed")
    await _assert_stream_mutable(e)
    at = aware(b.at) if b.at else now()
    if at > now():
        raise HTTPException(422, "Actual departure time cannot be in the future")
    rule = (e.get("requirements_snapshot") or {}).get("bed_release_rule", "after_effective_date")
    update = {"status": "departing", "actual_departure": at, "departure_by": u["sub"], "departure_note": b.note.strip()}
    changed = await db.exits.update_one({"_id": e["_id"], "status": "approved"}, {"$set": update})
    if not changed.modified_count:
        raise HTTPException(409, "Departure was already recorded")
    released = None
    reserved_bed_code = None
    try:
        if e["duration"] == "temporary":
            assignment = await db.assignments.find_one({"stream_id": e["stream_id"], "callup": e["callup"], "status": "active"})
            if assignment:
                bed = await db.beds.update_one({"stream_id": e["stream_id"], "code": assignment["bed_code"], "occupant": e["callup"], "status": "occupied"}, {"$set": {"status": "reserved"}})
                if not bed.modified_count and await db.beds.find_one({"stream_id": e["stream_id"], "code": assignment["bed_code"], "status": "reserved", "occupant": e["callup"]}) is None:
                    raise HTTPException(409, "The assigned bed could not be reserved")
                reserved_bed_code = assignment["bed_code"]
        elif e["duration"] == "remaining_period" and rule != "retain":
            effective = e.get("effective_date", e["depart_at"])
            if rule == "after_effective_date" and (at < effective or now() < effective):
                raise HTTPException(409, "The approved effective bed-release date has not been reached")
            released = await release_bed(e["callup"], e["stream_id"], u["sub"], "approved remaining-period exit", event="released_exit")
        await db.members.update_one({"stream_id": e["stream_id"], "callup": e["callup"]}, {"$set": {"in_camp": False}})
        finalized = await db.exits.update_one({"_id": e["_id"], "status": "departing"}, {"$set": {"status": "departed", "released_bed": released["bed_code"] if released else None}})
        if not finalized.modified_count:
            raise HTTPException(409, "This exit request changed during departure recording")
    except Exception:
        if reserved_bed_code:
            await db.beds.update_one({"stream_id": e["stream_id"], "code": reserved_bed_code, "occupant": e["callup"], "status": "reserved"}, {"$set": {"status": "occupied"}})
        if released:
            await db.beds.update_one({"stream_id": e["stream_id"], "code": released["bed_code"], "status": "available", "occupant": None}, {"$set": {"status": "occupied", "occupant": e["callup"]}})
            await db.assignments.update_one({"_id": released["_id"], "status": "released"}, {"$set": {"status": "active"}, "$unset": {"released_at": "", "release_reason": ""}})
            await db.allocation_history.insert_one({"stream_id": e["stream_id"], "callup": e["callup"], "event": "exit_release_rollback", "bed_code": released["bed_code"], "by": u["sub"], "reason": "Departure could not be finalized", "at": now()})
        await db.members.update_one({"stream_id": e["stream_id"], "callup": e["callup"]}, {"$set": {"in_camp": True}})
        await db.exits.update_one({"_id": e["_id"], "status": "departing"}, {"$set": {"status": "approved"}})
        raise
    await audit(u["sub"], "exit.depart", str(e["_id"]), {"callup": e["callup"], "duration": e["duration"], "released_bed": released["bed_code"] if released else None, "note": b.note.strip()})
    await notify(e["callup"], e["stream_id"], "Departure recorded", "Your approved departure from camp has been recorded.", "exit_departure")
    await notify(DIRECTOR, e["stream_id"], "Departure recorded", "An approved exit departure was recorded.", "exit_departure")
    return {"ok": True, "status": "departed", "released_bed": released["bed_code"] if released else None}


@router.post("/{eid}/return")
async def returned(eid: str, b: Mark, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid), "status": {"$in": ["departed", "overdue"]}})
    if not e:
        raise HTTPException(409, "No outstanding temporary exit to close")
    await _assert_stream_mutable(e)
    at = aware(b.at) if b.at else now()
    if at > now():
        raise HTTPException(422, "Actual return time cannot be in the future")
    if e.get("actual_departure") and at < e["actual_departure"]:
        raise HTTPException(422, "Return time cannot be before the recorded departure")
    assignment = await db.assignments.find_one({"stream_id": e["stream_id"], "callup": e["callup"], "status": "active"})
    returned_bed = assignment.get("bed_code") if assignment else None
    if assignment:
        bed = await db.beds.update_one({"stream_id": e["stream_id"], "code": assignment["bed_code"], "occupant": e["callup"], "status": "reserved"}, {"$set": {"status": "occupied"}})
        if not bed.modified_count and await db.beds.find_one({"stream_id": e["stream_id"], "code": assignment["bed_code"], "status": "occupied", "occupant": e["callup"]}) is None:
            raise HTTPException(409, "The reserved bed could not be restored; ask the Camp Director to review it")
    else:
        member = await db.members.find_one({"stream_id": e["stream_id"], "callup": e["callup"]})
        if member:
            assignment = await auto_assign_bed(member, u["sub"])
            returned_bed = assignment.get("bed_code") if assignment else None
    changed = await db.exits.update_one({"_id": e["_id"], "status": {"$in": ["departed", "overdue"]}}, {"$set": {
        "status": "returned", "actual_return": at, "returned_bed": returned_bed,
        "return_by": u["sub"], "return_note": b.note.strip(), "updated_at": now()}})
    if not changed.modified_count:
        raise HTTPException(409, "The return was already recorded")
    await db.members.update_one({"stream_id": e["stream_id"], "callup": e["callup"]}, {"$set": {"in_camp": True}})
    # Recording the member's return closes the active request. No second
    # Director completion step should block a later request.
    await db.exit_request_locks.delete_one({"stream_id": e["stream_id"], "callup": e["callup"]})
    await audit(u["sub"], "exit.return", str(e["_id"]), {"callup": e["callup"], "note": b.note.strip()})
    await notify(e["callup"], e["stream_id"], "Return recorded", "Your return to camp has been recorded.", "exit_return")
    await notify(DIRECTOR, e["stream_id"], "Member returned", "A corps member's return to camp was recorded.", "exit_return")
    return {"ok": True, "status": "returned", "bed_assigned": bool(returned_bed), "bed_code": returned_bed}


class CompleteIn(BaseModel):
    note: str = ""


@router.post("/{eid}/complete")
async def complete(eid: str, b: CompleteIn, u=Depends(require_director)):
    e = await db.exits.find_one({"_id": oid(eid), "status": {"$in": ["returned", "departed"]}})
    if not e:
        raise HTTPException(409, "Only a returned temporary exit or recorded permanent departure can be completed")
    await _assert_stream_mutable(e)
    changed = await db.exits.update_one({"_id": e["_id"], "status": e["status"]}, {"$set": {"status": "completed", "completed_at": now(), "completed_by": u["sub"], "completion_note": b.note.strip()}})
    if not changed.modified_count:
        raise HTTPException(409, "This request was updated by another user")
    await db.exit_request_locks.delete_one({"stream_id": e["stream_id"], "callup": e["callup"]})
    await audit(u["sub"], "exit.complete", str(e["_id"]), {"note": b.note.strip()})
    return {"ok": True, "status": "completed"}


@router.post("/{eid}/cancel")
async def cancel(eid: str, m=Depends(require_member)):
    e = await db.exits.find_one({"_id": oid(eid), "stream_id": m["stream_id"], "callup": m["callup"]})
    if not e or e["status"] not in ("draft", "submitted", "awaiting_platoon_confirmation", "awaiting_document_review", "awaiting_director_decision", "more_info_required"):
        raise HTTPException(409, "Only drafts or requests not yet approved can be cancelled")
    await _assert_stream_mutable(e)
    if e["status"] != "draft" and not e.get("member_can_cancel", True):
        raise HTTPException(403, "This exit category does not allow member cancellation after submission")
    changed = await db.exits.update_one({"_id": e["_id"], "status": e["status"]}, {"$set": {"status": "cancelled", "closed_at": now(), "cancelled_by": m["callup"]}})
    if not changed.modified_count:
        raise HTTPException(409, "This request was updated by another user")
    await db.exit_request_locks.delete_one({"stream_id": e["stream_id"], "callup": e["callup"]})
    await audit(m["callup"], "exit.cancel", str(e["_id"]))
    await notify(DIRECTOR, e["stream_id"], "Exit request cancelled", "A corps member cancelled an exit request.", "exit_cancelled")
    return {"status": "cancelled"}


async def mark_overdue(stream_id=None):
    f = {"duration": "temporary", "status": "departed", "return_at": {"$lt": now()}}
    if stream_id:
        f["stream_id"] = stream_id
    async for e in db.exits.find(f):
        stream = await db.streams.find_one({"_id": e["stream_id"]}, {"status": 1})
        if not stream or stream.get("status") == "closed":
            continue
        r = await db.exits.update_one({"_id": e["_id"], "status": "departed"}, {"$set": {"status": "overdue", "overdue_at": now()}})
        if not r.modified_count:
            continue
        await db.exits.update_one({"_id": e["_id"]}, {"$push": {"log": {"event": "overdue", "by": "system", "at": now()}}})
        await audit("system", "exit.overdue", str(e["_id"]), {"callup": e["callup"]})
        await notify(DIRECTOR, e["stream_id"], "Overdue return", "A corps member is overdue to return.", "overdue", dedupe_key=f"overdue:{e['_id']}")
        await notify(e["callup"], e["stream_id"], "Return reminder", "Your approved return time has passed. Please report to the camp.", "overdue", dedupe_key=f"overdue-m:{e['_id']}")
