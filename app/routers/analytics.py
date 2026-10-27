from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from ..db import db
from ..security import require_director
from ..util import now, oid
from .exits import mark_overdue

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _range(f: dict, field: str, d1, d2):
    if d1 or d2:
        f[field] = {**({"$gte": d1} if d1 else {}), **({"$lte": d2} if d2 else {})}
    return f


async def _group(col, match, key):
    return {(x["_id"] or "unknown"): x["n"] async for x in db[col].aggregate([{"$match": match}, {"$group": {"_id": key, "n": {"$sum": 1}}}])}


async def registration(sid, q):
    m = _range({"stream_id": sid}, "registered_at", q["d1"], q["d2"])
    if q["status"]:
        m["registration_status"] = q["status"]
    trend = [{"date": x["_id"], "count": x["n"]} async for x in db.members.aggregate([{"$match": m}, {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$registered_at"}}, "n": {"$sum": 1}}}, {"$sort": {"_id": 1}}])]
    return {"total": await db.members.count_documents(m), "by_status": await _group("members", m, "$registration_status"),
            "by_biometric_status": await _group("members", m, "$biometric_status"), "trend": trend,
            "by_stream": await _group("members", {"stream_id": sid}, "$stream_label"),
            "by_state": await _group("members", m, "$state"),
            "exceptions_pending": await db.notifications.count_documents({"stream_id": sid, "kind": "registration_exception", "recipient": "DIRECTOR", "read": False})}


async def hostel(sid, q):
    base = {"stream_id": sid, **({"hostel_code": q["hostel"].upper()} if q["hostel"] else {})}
    beds = await _group("beds", base, "$status")
    cap = sum(v for k, v in beds.items() if k != "disabled")
    per = []
    async for h in db.hostels.find({"stream_id": sid, **({"code": q["hostel"].upper()} if q["hostel"] else {})}):
        c = await _group("beds", {"hostel_id": h["_id"]}, "$status")
        hc = sum(v for k, v in c.items() if k != "disabled")
        per.append({"hostel": h["code"], "capacity": hc, "occupied": c.get("occupied", 0), "reserved": c.get("reserved", 0), "available": c.get("available", 0),
                    "occupancy_rate": round((c.get("occupied", 0) + c.get("reserved", 0)) / hc * 100, 1) if hc else 0})
    ap = {"stream_id": sid, "status": "active", **({"hostel_code": q["hostel"].upper()} if q["hostel"] else {})}
    hist = _range({"stream_id": sid, **({"bed_code": {"$regex": f"^{q['hostel'].upper()}-"}} if q["hostel"] else {})}, "at", q["d1"], q["d2"])
    return {"hostels": len(per), "bunks": await db.beds.count_documents({**base, "position": "upper"}), "capacity": cap, "occupied": beds.get("occupied", 0),
            "reserved": beds.get("reserved", 0), "available": beds.get("available", 0), "by_position": await _group("assignments", ap, "$position"), "per_hostel": per,
            "history_events": await _group("allocation_history", hist, "$event"), "released_via_exit": await db.allocation_history.count_documents({**hist, "event": "released_exit"}),
            "definition": "Occupancy rate = (occupied + reserved) / capacity. Reserved beds are never counted as available."}


async def attendance(sid, q):
    m = {"stream_id": sid, **({"week": q["week"]} if q["week"] else {}), **({"final_status": q["status"]} if q["status"] else {})}
    _range(m, "submitted_at", q["d1"], q["d2"])
    by_week = {}
    async for x in db.attendance.aggregate([{"$match": {"stream_id": sid}}, {"$group": {"_id": {"w": "$week", "s": "$final_status"}, "n": {"$sum": 1}}}]):
        by_week.setdefault(x["_id"]["w"], {})[x["_id"]["s"]] = x["n"]
    members = await db.members.count_documents({"stream_id": sid, "registration_status": {"$ne": "rejected"}})
    weeks = [{"week": w, "statuses": s, "attendance_rate": round((s.get("verified_present", 0) + s.get("admin_marked_present", 0)) / members * 100, 1) if members else 0} for w, s in sorted(by_week.items())]
    face = await _group("attendance", {**m, "face": {"$ne": None}}, "$face.result")
    loc = await _group("attendance", {**m, "location.result": {"$in": ["passed", "failed", "review"]}}, "$location.result")
    ft, lt = sum(face.values()), sum(loc.values())
    not_submitted = None
    if q["week"]:
        got = {r["callup"] async for r in db.attendance.find({"stream_id": sid, "week": q["week"], "submitted_at": {"$ne": None}}, {"callup": 1})}
        allm = {r["callup"] async for r in db.members.find({"stream_id": sid, "in_camp": True, "registration_status": {"$ne": "rejected"}}, {"callup": 1})}
        not_submitted = sorted(allm - got)[:200]
    return {"members_expected": members, "weeks": weeks, "final_status": await _group("attendance", m, "$final_status"), "source": await _group("attendance", m, "$status_source"),
            "automated_results": {"face": face, "location": loc}, "face_pass_rate": round(face.get("passed", 0) / ft * 100, 1) if ft else None,
            "location_pass_rate": round(loc.get("passed", 0) / lt * 100, 1) if lt else None, "pending_manual_review": await db.attendance.count_documents({**m, "final_status": "pending_manual_review"}),
            "not_submitted": not_submitted, "definition": "Rate = (verified_present + admin_marked_present) / registered members (excluding rejected)."}


async def exits(sid, q):
    await mark_overdue(sid)
    m = _range({"stream_id": sid, **({"status": q["status"]} if q["status"] else {}), **({"type": q["type"]} if q["type"] else {})}, "created_at", q["d1"], q["d2"])
    return {"by_status": await _group("exits", m, "$status"), "by_type": await _group("exits", m, "$type"), "currently_away": await db.members.count_documents({"stream_id": sid, "in_camp": False}),
            "overdue": await db.exits.count_documents({"stream_id": sid, "status": "overdue"}), "beds_released": await db.allocation_history.count_documents({"stream_id": sid, "event": "released_exit"})}


async def complaints(sid, q):
    m = _range({"stream_id": sid, **({"category": q["category"]} if q["category"] else {}), **({"status": q["status"]} if q["status"] else {})}, "created_at", q["d1"], q["d2"])
    rt = [(x["resolved_at"] - x["created_at"]).total_seconds() async for x in db.complaints.find({**m, "resolved_at": {"$exists": True}})]
    openf = {**m, "status": {"$in": ["open", "in_review"]}}
    n_total = await db.notifications.count_documents({"stream_id": sid})
    failed = await db.notifications.count_documents({"stream_id": sid, "delivery": {"$elemMatch": {"ok": False}}})
    return {"by_category": await _group("complaints", m, "$category"), "by_status": await _group("complaints", m, "$status"), "avg_resolution_hours": round(sum(rt) / len(rt) / 3600, 1) if rt else None,
            "outstanding": await db.complaints.count_documents(openf), "overdue_over_7_days": await db.complaints.count_documents({**openf, "created_at": {"$lt": now() - timedelta(days=7)}}),
            "notifications": {"total": n_total, "with_failed_delivery": failed, "failure_rate": round(failed / n_total * 100, 1) if n_total else None}}


@router.get("/{section}")
async def get_section(section: str, stream_id: str, week: int | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
                      hostel_code: str | None = None, status: str | None = None, type: str | None = None, category: str | None = None, u=Depends(require_director)):
    sid = oid(stream_id)
    q = {"week": week, "d1": date_from, "d2": date_to, "hostel": hostel_code, "status": status, "type": type, "category": category}
    fns = {"registration": registration, "hostel": hostel, "attendance": attendance, "exits": exits, "complaints": complaints}
    if section not in fns:
        raise HTTPException(404, "Unknown analytics section")
    return {**(await fns[section](sid, q)), "generated_at": now().isoformat()}
