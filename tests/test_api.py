"""Integration tests. Need a reachable MongoDB (MONGO_URI, default localhost). Uses database `nysc_test`, wiped per run.
Run:  cd backend && MONGO_DB=nysc_test pytest -q"""
import asyncio
import os

os.environ["MONGO_DB"] = "nysc_test"
os.environ.setdefault("JWT_SECRET", "test-secret")

import httpx
import pytest
import pytest_asyncio
from bson import ObjectId

from app.db import client, db, ensure_indexes
from app.main import app
from app.security import hash_pw, make_token
from app.util import now


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def api():
    try:
        await asyncio.wait_for(client.admin.command("ping"), 3)
    except Exception:
        pytest.skip("MongoDB not reachable")
    await client.drop_database("nysc_test")
    await ensure_indexes()
    await db.users.insert_one({"username": "director", "password_hash": hash_pw("pw"), "role": "director"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def H(role, sub):
    return {"Authorization": f"Bearer {make_token(sub, role)}"}


D = H("director", "director")


async def mk_member(callup, stream, gender="M", name="Test Member"):
    await db.members.insert_one({"callup": callup, "stream_id": stream["_id"], "stream_label": stream["label"], "full_name": name, "gender": gender, "state": "Oyo", "phone": "0800", "email": "",
                                 "password_hash": "x", "enrollment": {"front": "f", "side": "s"}, "registration_status": "verified", "biometric_status": "pending_review", "in_camp": True, "registered_at": now()})


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def stream(api):
    r = await api.post("/api/streams", json={"year": 2026, "batch": "C", "number": 1, "status": "active"}, headers=D)
    assert r.status_code == 200
    s = await db.streams.find_one({"label": "2026 Batch C Stream 1"})
    assert (await api.post("/api/hostels", json={"stream_id": str(s["_id"]), "code": "M1", "bunk_count": 2, "gender": "M"}, headers=D)).status_code == 200
    return s


async def test_authorization(api, stream):
    m = H("member", "KG/26C/0001")
    assert (await api.post("/api/streams", json={"year": 2027, "batch": "A", "number": 1}, headers=m)).status_code in (401, 403)
    assert (await api.get("/api/members", headers=m)).status_code in (401, 403)
    assert (await api.get("/api/analytics/hostel", params={"stream_id": str(stream["_id"])})).status_code == 401


async def test_concurrent_assignment_single_winner(api, stream):
    for i in range(5):
        await mk_member(f"KG/26C/10{i}", stream)
    res = await asyncio.gather(*[api.post("/api/assignments", json={"callup": f"KG/26C/10{i}", "bed_code": "M1-001-U"}, headers=D) for i in range(5)])
    assert sorted(r.status_code for r in res).count(200) == 1
    assert await db.assignments.count_documents({"bed_code": "M1-001-U", "status": "active"}) == 1


async def test_exit_workflow_separates_submission_approval_and_departure(api, stream):
    await mk_member("KG/26C/200", stream)
    await mk_member("KG/26C/201", stream)
    assert (await api.post("/api/assignments", json={"callup": "KG/26C/200", "bed_code": "M1-002-U"}, headers=D)).status_code == 200
    assert (await api.post("/api/assignments", json={"callup": "KG/26C/201", "bed_code": "M1-002-L"}, headers=D)).status_code == 200
    mem = H("member", "KG/26C/200")
    category = await api.post("/api/exits/categories", json={"stream_id": str(stream["_id"]), "name": "Family visit", "duration_options": ["temporary"], "require_platoon_confirmation": False, "require_document_review": False, "document_types": []}, headers=D)
    assert category.status_code == 200
    r = await api.post("/api/exits", json={"category_id": category.json()["id"], "duration": "temporary", "reason": "family visit", "depart_at": "2030-01-01T10:00:00Z", "return_at": "2030-01-03T10:00:00Z"}, headers=mem)
    assert r.status_code == 200
    eid = r.json()["id"]
    assert (await api.post(f"/api/exits/{eid}/depart", json={}, headers=D)).status_code == 409
    assert (await api.post(f"/api/exits/{eid}/submit", headers=mem)).status_code == 200
    assert (await api.post(f"/api/exits/{eid}/decide", json={"approve": True, "reason": "Approved family visit"}, headers=D)).status_code == 200
    assert (await api.post(f"/api/exits/{eid}/depart", json={}, headers=D)).status_code == 200
    assert (await db.beds.find_one({"code": "M1-002-U"}))["status"] == "reserved"
    # reserved bed cannot be handed out
    await mk_member("KG/26C/202", stream)
    assert (await api.post("/api/assignments", json={"callup": "KG/26C/202", "bed_code": "M1-002-U"}, headers=D)).status_code == 409
    assert (await api.post(f"/api/exits/{eid}/return", json={}, headers=D)).status_code == 200
    assert (await db.beds.find_one({"code": "M1-002-U"}))["status"] == "occupied"


async def test_platoon_mapping_and_exceptions_are_configurable(api, stream):
    await mk_member("KG/26C/501", stream)
    config = await api.put("/api/platoons/config", json={"stream_id": str(stream["_id"]), "digit_mapping": {"7": 10}}, headers=D)
    assert config.status_code == 200
    member_token = H("member", "KG/26C/501")
    saved_code = await api.put("/api/members/me/state-code", json={"state_code": "007"}, headers=member_token)
    assert saved_code.status_code == 200 and saved_code.json()["platoon_number"] == 10
    assignment = await db.platoon_assignments.find_one({"callup": "KG/26C/501", "status": "active"})
    assert assignment["platoon_number"] == 10 and assignment["state_code"] == "007"
    assert (await api.get("/api/platoons?stream_id=" + str(stream["_id"]), headers=H("member", "KG/26C/501"))).status_code == 403
    assert (await api.put("/api/members/me/state-code", json={"state_code": "008"}, headers=member_token)).status_code == 409
    correction = await api.post("/api/platoons/assign", json={"stream_id": str(stream["_id"]), "callup": "KG/26C/501", "platoon_number": 3, "reason": "Director correction"}, headers=D)
    assert correction.status_code == 200
    assert await db.platoon_assignments.count_documents({"callup": "KG/26C/501", "status": "active"}) == 1
    assert (await db.platoon_assignments.find_one({"callup": "KG/26C/501", "status": "active"}))["platoon_number"] == 3
    assert (await api.get("/api/platoons/history", params={"stream_id": str(stream["_id"]), "callup": "KG/26C/501"}, headers=D)).json()[0]["reason"] == "Director correction"
    assert (await api.post("/api/platoons/assign", json={"stream_id": str(stream["_id"]), "callup": "KG/26C/501", "platoon_number": 2, "reason": "Member should not do this"}, headers=member_token)).status_code == 403


async def test_exit_evidence_alternatives_review_gate_and_requirement_snapshot(api, stream):
    await mk_member("KG/26C/601", stream)
    ids = ["alternative-a", "alternative-b", "optional-note"]
    category_payload = {"stream_id": str(stream["_id"]), "name": "Evidence workflow", "duration_options": ["temporary"],
        "require_platoon_confirmation": False, "require_document_review": True,
        "document_types": [
            {"id": ids[0], "name": "Evidence A", "purpose": "Establishes the request details", "required": False},
            {"id": ids[1], "name": "Evidence B", "purpose": "Alternative proof for the request", "required": False},
            {"id": ids[2], "name": "Optional note", "purpose": "Additional context", "required": False}],
        "required_document_groups": [[ids[0], ids[1]]]}
    category = await api.post("/api/exits/categories", json=category_payload, headers=D)
    assert category.status_code == 200
    mem = H("member", "KG/26C/601")
    draft = await api.post("/api/exits", json={"category_id": category.json()["id"], "duration": "temporary", "reason": "Supporting evidence", "depart_at": "2030-02-01T10:00:00Z", "return_at": "2030-02-02T10:00:00Z"}, headers=mem)
    eid = draft.json()["id"]
    assert (await api.post(f"/api/exits/{eid}/submit", headers=mem)).status_code == 422  # one of the alternatives is required
    document = {"id": "upload-1", "document_type_id": ids[0], "document_type_name": "Evidence A", "file_ref": "private/test.pdf",
        "filename": "test.pdf", "mime_type": "application/pdf", "size_bytes": 10, "uploaded_at": now(), "review_status": "pending_review", "review_history": []}
    await db.exits.update_one({"_id": ObjectId(eid)}, {"$push": {"documents": document}})
    submitted = await api.post(f"/api/exits/{eid}/submit", headers=mem)
    assert submitted.status_code == 200 and submitted.json()["status"] == "awaiting_document_review"
    assert (await api.post(f"/api/exits/{eid}/decide", json={"approve": True, "reason": "Normal approval"}, headers=D)).status_code == 409
    assert (await api.get(f"/api/exits/{eid}/documents/upload-1/file", headers=mem)).status_code == 403
    changed = await api.post(f"/api/exits/{eid}/documents/upload-1/review", json={"decision": "accepted", "notes": "Evidence accepted"}, headers=D)
    assert changed.status_code == 200
    requested = await api.post(f"/api/exits/{eid}/request-info", json={"document_type_ids": [ids[0]], "reason": "Please provide a current copy"}, headers=D)
    assert requested.status_code == 200
    assert (await api.post(f"/api/exits/{eid}/submit", headers=mem)).status_code == 422  # prior file does not satisfy a new evidence request
    replacement = {**document, "id": "upload-2", "uploaded_at": now(), "review_status": "pending_review", "replaces_document_id": "upload-1"}
    await db.exits.update_one({"_id": ObjectId(eid)}, {"$push": {"documents": replacement}})
    submitted_again = await api.post(f"/api/exits/{eid}/submit", headers=mem)
    assert submitted_again.status_code == 200 and submitted_again.json()["status"] == "awaiting_document_review"
    reviewed_again = await api.post(f"/api/exits/{eid}/documents/upload-2/review", json={"decision": "accepted", "notes": "Current copy accepted"}, headers=D)
    assert reviewed_again.status_code == 200
    updated_payload = {**category_payload, "document_types": [{"id": "new-rule", "name": "New requirement", "purpose": "Applies to later requests", "required": True}] , "required_document_groups": []}
    updated = await api.put(f"/api/exits/categories/{category.json()['id']}", json=updated_payload, headers=D)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    detail = await api.get(f"/api/exits/{eid}", headers=D)
    assert detail.json()["requirements_snapshot"]["version"] == 1
    assert detail.json()["requirements_snapshot"]["document_types"][0]["id"] == ids[0]
    decision = await api.post(f"/api/exits/{eid}/decide", json={"approve": True, "reason": "Required evidence checked"}, headers=D)
    assert decision.status_code == 200 and decision.json()["status"] == "approved"


async def test_urgent_medical_exit_escalates_without_waiting_for_routine_evidence(api, stream):
    await mk_member("KG/26C/602", stream)
    category = await api.post("/api/exits/categories", json={"stream_id": str(stream["_id"]), "name": "Urgent pathway",
        "duration_options": ["temporary"], "emergency_medical": True, "require_platoon_confirmation": True,
        "document_types": [{"id": "medical-note", "name": "Medical note", "purpose": "Follow-up evidence", "required": True}]}, headers=D)
    assert category.status_code == 200
    mem = H("member", "KG/26C/602")
    from datetime import timedelta
    departure = now() - timedelta(minutes=1)
    draft = await api.post("/api/exits", json={"category_id": category.json()["id"], "duration": "temporary", "reason": "Urgent care", "depart_at": departure.isoformat(), "return_at": (now() + timedelta(days=1)).isoformat(), "urgent": True}, headers=mem)
    assert draft.status_code == 200
    eid = draft.json()["id"]
    submitted = await api.post(f"/api/exits/{eid}/submit", headers=mem)
    assert submitted.status_code == 200 and submitted.json()["status"] == "awaiting_director_decision"
    assert (await api.post(f"/api/exits/{eid}/decide", json={"approve": True, "reason": "Emergency"}, headers=D)).status_code == 409
    approved = await api.post(f"/api/exits/{eid}/decide", json={"approve": True, "emergency": True, "reason": "Immediate medical care authorized"}, headers=D)
    assert approved.status_code == 200
    saved = await db.exits.find_one({"_id": ObjectId(eid)})
    assert saved["emergency_decision"]["outstanding_documents"] == ["Medical note (missing)"]


async def test_override_preserves_evidence_and_requires_reason(api, stream):
    sch = await api.post("/api/attendance/schedules", json={"stream_id": str(stream["_id"]), "week": 1, "opens_at": "2020-01-01T00:00:00Z", "closes_at": "2020-01-02T00:00:00Z"}, headers=D)
    sid = sch.json()["id"]
    sdoc = await db.attendance_schedules.find_one({"week": 1})
    await db.attendance.insert_one({"schedule_id": sdoc["_id"], "stream_id": stream["_id"], "callup": "KG/26C/100", "week": 1, "submitted_at": now(), "face": {"result": "failed", "score": 20}, "location": {"result": "not_evaluated"},
                                    "final_status": "face_failed", "status_source": "automatic", "overrides": []})
    bad = await api.post("/api/attendance/override", json={"schedule_id": sid, "callup": "KG/26C/100", "new_status": "admin_marked_present", "reason": ""}, headers=D)
    assert bad.status_code == 422
    ok = await api.post("/api/attendance/override", json={"schedule_id": sid, "callup": "KG/26C/100", "new_status": "admin_marked_present", "reason": "Camera fault confirmed"}, headers=D)
    assert ok.status_code == 200
    a = await db.attendance.find_one({"callup": "KG/26C/100", "week": 1})
    assert a["final_status"] == "admin_marked_present" and a["status_source"] == "administrative_override"
    assert a["face"]["result"] == "failed" and a["face"]["score"] == 20 and a["overrides"][0]["from"] == "face_failed"


async def test_csv_formula_injection(api, stream):
    await mk_member("KG/26C/300", stream, name="=HYPERLINK(\"http://x\")")
    r = await api.get("/api/reports/members.csv", params={"stream_id": str(stream["_id"])}, headers=D)
    assert r.status_code == 200 and "'=HYPERLINK" in r.text and ',"=HYPERLINK' not in r.text


async def test_closed_stream_is_read_only(api, stream):
    s2 = (await api.post("/api/streams", json={"year": 2026, "batch": "C", "number": 2, "status": "active"}, headers=D)).json()
    assert (await api.patch(f"/api/streams/{s2['id']}/status", json={"status": "closed"}, headers=D)).status_code == 200
    assert (await api.patch(f"/api/streams/{s2['id']}/status", json={"status": "active"}, headers=D)).status_code == 409
    assert (await api.post("/api/hostels", json={"stream_id": s2["id"], "code": "X1", "bunk_count": 1, "gender": "M"}, headers=D)).status_code == 409
