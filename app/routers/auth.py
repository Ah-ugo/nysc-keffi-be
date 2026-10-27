from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db import db
from ..security import current_user, make_token, rate_limit, verify_pw
from ..services.rules import normalise_callup
from ..util import ser

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Login(BaseModel):
    identifier: str  # director username or member call-up number
    password: str


@router.post("/login", dependencies=[Depends(rate_limit(10, 60))])
async def login(b: Login):
    u = await db.users.find_one({"username": b.identifier.strip().lower()})
    if u and verify_pw(b.password, u["password_hash"]):
        if u.get("role") == "platoon_officer":
            if not u.get("active") or not u.get("platoon_id"):
                raise HTTPException(403, "This officer account is not currently assigned to a platoon")
            role = "platoon_officer"
        elif u.get("role") == "director":
            role = "director"
        else:
            raise HTTPException(401, "This account does not have an active application role")
        return {"token": make_token(u["username"], role), "role": role}
    m = await db.members.find_one({"callup": normalise_callup(b.identifier)})
    if m and verify_pw(b.password, m["password_hash"]):
        return {"token": make_token(m["callup"], "member"), "role": "member"}
    raise HTTPException(401, "Incorrect call-up number/username or password")


@router.get("/me")
async def me(u=Depends(current_user)):
    if u["role"] == "member":
        m = await db.members.find_one({"callup": u["sub"]})
        return {"role": "member", "profile": ser(m)}
    if u["role"] == "platoon_officer":
        officer = await db.users.find_one({"username": u["sub"], "role": "platoon_officer", "active": True, "platoon_id": {"$exists": True}})
        if not officer:
            raise HTTPException(403, "This officer account is no longer assigned to a platoon")
        p = await db.platoons.find_one({"_id": officer["platoon_id"]})
        return {"role": "platoon_officer", "username": u["sub"], "platoon": p["number"] if p else None}
    return {"role": "director", "username": u["sub"]}
