import re

from fastapi import HTTPException

from ..db import db
from ..services.audit import audit
from ..services.notify import DIRECTOR, notify
from ..util import now


async def ensure_platoons(stream_id):
    for number in range(1, 11):
        await db.platoons.update_one(
            {"stream_id": stream_id, "number": number},
            {"$setOnInsert": {"stream_id": stream_id, "number": number, "officer_username": None,
                               "officer_name": "", "appointments": {"oic_parade": None, "platoon_leader": None},
                               "created_at": now()}}, upsert=True,
        )


def parse_state_code(value):
    value = str(value or "").strip()
    return value if re.fullmatch(r"\d{1,6}", value) else None


async def assign_member(member: dict, actor="system", reason="automatic assignment", notify_member=True):
    stream_id, callup = member["stream_id"], member["callup"]
    current = await db.platoon_assignments.find_one({"stream_id": stream_id, "callup": callup, "status": "active"})
    if current:
        return current
    state_code = parse_state_code(member.get("state_code"))
    cfg = await db.platoon_configs.find_one({"stream_id": stream_id})
    mapping = (cfg or {}).get("digit_mapping", {})
    target = mapping.get(state_code[-1]) if state_code else None
    if target not in range(1, 11):
        why = "Invalid or missing NYSC state code" if not state_code else f"No platoon mapping for state-code digit {state_code[-1]}"
        active_exception = await db.platoon_assignment_exceptions.find_one({"stream_id": stream_id, "callup": callup, "resolved": False}, {"_id": 1})
        await db.members.update_one({"_id": member["_id"]}, {"$set": {"platoon_assignment_status": "awaiting_assignment", "platoon_exception": why}})
        await db.platoon_assignment_exceptions.update_one(
            {"stream_id": stream_id, "callup": callup, "resolved": False},
            {"$set": {"stream_id": stream_id, "callup": callup, "state_code": member.get("state_code"),
                      "reason": why, "updated_at": now()}, "$setOnInsert": {"created_at": now(), "resolved": False}}, upsert=True,
        )
        if not active_exception:
            await notify(DIRECTOR, stream_id, "Platoon assignment needs review", "A member is awaiting a valid state-code mapping or director assignment.",
                         "platoon_exception", dedupe_key=f"platoon-exception:{stream_id}:{callup}:{now().timestamp()}")
        return None
    platoon = await db.platoons.find_one({"stream_id": stream_id, "number": target})
    if not platoon:
        await ensure_platoons(stream_id)
        platoon = await db.platoons.find_one({"stream_id": stream_id, "number": target})
    if not platoon:
        raise HTTPException(409, "The configured platoon does not exist")
    status = "active"
    if current:
        await db.platoon_assignments.update_one({"_id": current["_id"], "status": "active"},
                                                {"$set": {"status": "superseded", "ended_at": now(), "end_reason": reason}})
    assignment = {"stream_id": stream_id, "callup": callup, "platoon_number": target, "status": status,
                  "method": "state_code_mapping", "state_code": state_code,
                  "assigned_by": actor, "reason": reason, "assigned_at": now(), "platoon_id": platoon["_id"]}
    try:
        result = await db.platoon_assignments.insert_one(assignment)
        assignment["_id"] = result.inserted_id
    except Exception:
        existing = await db.platoon_assignments.find_one({"stream_id": stream_id, "callup": callup, "status": "active"})
        if existing:
            return existing
        if current:
            await db.platoon_assignments.update_one({"_id": current["_id"]},
                                                    {"$set": {"status": "active"}, "$unset": {"ended_at": "", "end_reason": ""}})
        return None
    await db.members.update_one({"_id": member["_id"]}, {"$set": {
        "platoon_assignment_status": "confirmed",
        "platoon_number": target,
        "platoon_exception": None,
    }})
    await db.platoon_assignment_exceptions.update_many({"stream_id": stream_id, "callup": callup, "resolved": False},
                                                       {"$set": {"resolved": True, "resolved_at": now()}})
    await db.allocation_history.insert_one({"stream_id": stream_id, "callup": callup, "event": "platoon_assigned" if not current else "platoon_reassigned",
                                            "platoon_number": target, "from_platoon": current["platoon_number"] if current else None,
                                            "by": actor, "reason": reason, "at": now()})
    await audit(actor, "platoon.assignment", callup, {"to": target, "from": current["platoon_number"] if current else None, "reason": reason})
    if notify_member:
        await notify(callup, stream_id, "Platoon assignment updated", "Your platoon assignment is ready. Open the app to view it.", "platoon")
    return assignment


async def activate_legacy_pending_assignments(stream_id, callup=None):
    """Move assignments from the old approval flow into automatic active status."""
    query = {"stream_id": stream_id, "status": "pending"}
    if callup:
        query["callup"] = callup
    async for assignment in db.platoon_assignments.find(query):
        active = await db.platoon_assignments.find_one(
            {"stream_id": stream_id, "callup": assignment["callup"], "status": "active"}, {"_id": 1}
        )
        if active:
            await db.platoon_assignments.update_one({"_id": assignment["_id"], "status": "pending"}, {"$set": {"status": "superseded", "ended_at": now(), "end_reason": "Automatic assignment flow"}})
            continue
        changed = await db.platoon_assignments.update_one(
            {"_id": assignment["_id"], "status": "pending"}, {"$set": {"status": "active"}}
        )
        if changed.modified_count:
            await db.members.update_one(
                {"stream_id": stream_id, "callup": assignment["callup"]},
                {"$set": {"platoon_assignment_status": "confirmed", "platoon_number": assignment["platoon_number"]}},
            )
