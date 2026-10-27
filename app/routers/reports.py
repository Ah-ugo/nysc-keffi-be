import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..db import db
from ..security import require_director, require_member
from ..services.audit import audit
from ..services.rules import csv_safe
from ..util import now, oid

router = APIRouter(prefix="/api/reports", tags=["reports"])
D = lambda v: v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else (v if v is not None else "")


async def dataset(kind: str, sid, week: int | None = None):
    st = await db.streams.find_one({"_id": sid})
    if not st:
        raise HTTPException(404, "Stream not found")
    title = f"{st['label']} - "
    if kind == "members":
        h = ["Call-up", "Name", "Gender", "State", "Phone", "Registration", "Biometric", "Registered"]
        r = [[m["callup"], m["full_name"], m["gender"], m["state"], m["phone"], m["registration_status"], m["biometric_status"], D(m["registered_at"])] async for m in db.members.find({"stream_id": sid}).sort("callup", 1)]
        return title + "Registration list", h, r
    if kind == "occupancy":
        names = {m["callup"]: m["full_name"] async for m in db.members.find({"stream_id": sid}, {"callup": 1, "full_name": 1})}
        h = ["Hostel", "Bunk", "Position", "Bed", "Status", "Occupant", "Name"]
        r = [[b["hostel_code"], b["bunk"], b["position"], b["code"], b["status"], b["occupant"] or "", names.get(b["occupant"], "")] async for b in db.beds.find({"stream_id": sid}).sort([("hostel_code", 1), ("bunk_no", 1), ("position", -1)])]
        return title + "Hostel occupancy", h, r
    if kind in ("attendance", "exceptions", "overrides"):
        f: dict = {"stream_id": sid}
        if week:
            f["week"] = week
        if kind == "exceptions":
            f["final_status"] = {"$in": ["pending_manual_review", "face_failed", "location_failed"]}
        if kind == "overrides":
            f["overrides.0"] = {"$exists": True}
        h = ["Week", "Call-up", "Final status", "Source", "Face result", "Face score", "Location result", "Distance (m)", "Last override reason", "Submitted"]
        r = []
        async for a in db.attendance.find(f).sort([("week", 1), ("callup", 1)]):
            fc, lc = a.get("face") or {}, a.get("location") or {}
            ov = a["overrides"][-1]["reason"] if a.get("overrides") else ""
            r.append([a["week"], a["callup"], a["final_status"], a["status_source"], fc.get("result", ""), fc.get("score", ""), lc.get("result", ""), round(lc["distance_m"]) if lc.get("distance_m") is not None else "", ov, D(a.get("submitted_at"))])
        return title + {"attendance": "Attendance sheet", "exceptions": "Attendance exceptions", "overrides": "Manual overrides"}[kind], h, r
    if kind == "exits":
        h = ["Call-up", "Name", "Type", "Status", "Depart", "Expected return", "Actual departure", "Actual return", "Bed released"]
        r = [[e["callup"], e["full_name"], e["type"], e["status"], D(e["depart_at"]), D(e.get("return_at")), D(e.get("actual_departure")), D(e.get("actual_return")), e.get("released_bed", "")] async for e in db.exits.find({"stream_id": sid}).sort("created_at", -1)]
        return title + "Exit requests and returns", h, r
    raise HTTPException(404, "Unknown report")


@router.get("/{kind}.csv")
async def csv_report(kind: str, stream_id: str, week: int | None = None, u=Depends(require_director)):
    title, h, rows = await dataset(kind, oid(stream_id), week)
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow([csv_safe(x) for x in h])
    for r in rows:
        w.writerow([csv_safe(x) for x in r])
    await audit(u["sub"], "report.csv", kind, {"stream": stream_id})
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{kind}.csv"', "Cache-Control": "no-store"})


def _pdf(title: str, headers, rows, landscape_mode=True) -> bytes:
    buf = io.BytesIO()
    ps = landscape(A4) if landscape_mode else A4

    def footer(c, d):
        c.setFont("Helvetica", 8)
        c.drawString(30, 18, f"Generated {now().strftime('%Y-%m-%d %H:%M UTC')} - NYSC Keffi Orientation Camp")
        c.drawRightString(ps[0] - 30, 18, f"Page {d.page}")

    doc = SimpleDocTemplate(buf, pagesize=ps, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=36, title=title)
    st = getSampleStyleSheet()
    cell = st["BodyText"]; cell.fontSize = 7; cell.leading = 9
    data = [[Paragraph(f"<b>{x}</b>", cell) for x in headers]] + [[Paragraph(str(c).replace("&", "&amp;").replace("<", "&lt;"), cell) for c in r] for r in rows]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9e8dc")), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    doc.build([Paragraph(title, st["Title"]), Spacer(1, 6), Paragraph(f"{len(rows)} records", st["Normal"]), Spacer(1, 8), t], onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


@router.get("/{kind}.pdf")
async def pdf_report(kind: str, stream_id: str, week: int | None = None, u=Depends(require_director)):
    title, h, rows = await dataset(kind, oid(stream_id), week)
    await audit(u["sub"], "report.pdf", kind, {"stream": stream_id})
    return Response(_pdf(title, h, rows), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{kind}.pdf"', "Cache-Control": "no-store"})


@router.get("/my-assignment.pdf")
async def my_assignment_pdf(m=Depends(require_member)):
    a = await db.assignments.find_one({"stream_id": m["stream_id"], "callup": m["callup"], "status": "active"})
    if not a:
        raise HTTPException(404, "No active assignment")
    rows = [["Full name", m["full_name"]], ["Call-up number", m["callup"]], ["Batch / Stream", m["stream_label"]], ["Hostel", a["hostel_code"]],
            ["Bunk", a["bunk"]], ["Sleeping position", a["position"].title()], ["Status", "Active"], ["Assigned on", D(a["assigned_at"])]]
    return Response(_pdf("Hostel Assignment Slip", ["Item", "Detail"], rows, False), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="assignment.pdf"', "Cache-Control": "no-store"})


@router.get("/audit-log.csv")
async def audit_csv(u=Depends(require_director)):
    buf = io.StringIO(); w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(["At", "Actor", "Action", "Target", "Detail"])
    async for a in db.audit_logs.find().sort("at", -1).limit(5000):
        w.writerow([csv_safe(D(a["at"])), csv_safe(a["actor"]), csv_safe(a["action"]), csv_safe(a["target"]), csv_safe(a["detail"])])
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'})
