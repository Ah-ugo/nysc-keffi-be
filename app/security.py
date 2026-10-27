import time
from collections import defaultdict, deque
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from .config import settings
from .db import db
from .util import now

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)


def hash_pw(p: str) -> str:
    return pwd.hash(p)


def verify_pw(p: str, h: str) -> bool:
    return pwd.verify(p, h)


def make_token(sub: str, role: str) -> str:
    exp = now() + timedelta(minutes=settings.jwt_minutes)
    return jwt.encode({"sub": sub, "role": role, "exp": exp}, settings.jwt_secret, algorithm="HS256")


async def current_user(cred: HTTPAuthorizationCredentials = Depends(bearer)):
    if not cred:
        raise HTTPException(401, "Not authenticated")
    try:
        p = jwt.decode(cred.credentials, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired session")
    if p.get("role") not in ("director", "platoon_officer", "member"):
        raise HTTPException(401, "This account role is not supported")
    return {"sub": p["sub"], "role": p["role"]}


async def require_director(u=Depends(current_user)):
    if u["role"] != "director":
        raise HTTPException(403, "Camp Director access only")
    return u


async def require_member(u=Depends(current_user)):
    """Returns the member document. Member identity always comes from the token, never from the request."""
    if u["role"] != "member":
        raise HTTPException(403, "Corps member access only")
    m = await db.members.find_one({"callup": u["sub"]})
    if not m:
        raise HTTPException(401, "Member not found")
    return m


async def require_platoon_officer(u=Depends(current_user)):
    if u["role"] != "platoon_officer":
        raise HTTPException(403, "Assigned Platoon Officer access only")
    officer = await db.users.find_one({"username": u["sub"], "role": "platoon_officer", "active": True, "platoon_id": {"$exists": True}})
    if not officer:
        raise HTTPException(403, "This officer account is not currently assigned to a platoon")
    return {**u, "platoon_id": officer["platoon_id"]}


async def require_director_or_platoon_officer(u=Depends(current_user)):
    if u["role"] == "director":
        return u
    return await require_platoon_officer(u)


async def active_stream(stream_id) -> dict:
    s = await db.streams.find_one({"_id": stream_id})
    if not s or s["status"] != "active":
        raise HTTPException(409, "This stream is not active; operation not allowed")
    return s


# Simple in-memory rate limiter (single-process). Use Redis/nginx limits in multi-instance production.
_hits = defaultdict(deque)


def rate_limit(max_calls: int, per_seconds: int):
    async def dep(request: Request):
        key = f"{request.url.path}:{request.client.host if request.client else 'x'}"
        q, t = _hits[key], time.time()
        while q and q[0] < t - per_seconds:
            q.popleft()
        if len(q) >= max_calls:
            raise HTTPException(429, "Too many requests, try again shortly")
        q.append(t)
    return dep
