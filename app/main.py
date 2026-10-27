import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure
from fastapi.middleware.cors import CORSMiddleware

from . import jobs
from .config import settings
from .db import db, ensure_indexes
from .routers import analytics, attendance, auth, engage, exits, hostels, members, platoons, push, reports, streams, swaps
from .security import hash_pw
from .util import now


@asynccontextmanager
async def lifespan(app):
    await ensure_indexes()
    if not await db.users.find_one({"username": settings.director_username}):
        await db.users.insert_one({"username": settings.director_username, "password_hash": hash_pw(settings.director_password), "role": "director", "created_at": now()})
    task = asyncio.create_task(jobs.loop())
    yield
    task.cancel()


app = FastAPI(title="NYSC Keffi Camp Management", lifespan=lifespan)


@app.exception_handler(ConnectionFailure)
async def database_unavailable(request: Request, exc: ConnectionFailure):
    # Keep driver details (including hostnames) in server logs, not API responses.
    return JSONResponse(
        status_code=503,
        content={"detail": "The camp system cannot reach its database right now. Please try again shortly. If this continues, contact your system administrator."},
    )


app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])
for r in (auth, streams, members, hostels, platoons, swaps, exits, attendance, engage, analytics, reports, push):
    app.include_router(r.router)


@app.get("/api/health")
async def health():
    await db.command("ping")
    return {"ok": True, "database": "connected", "face_provider": settings.face_provider}
