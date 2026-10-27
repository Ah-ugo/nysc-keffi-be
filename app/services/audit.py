from ..db import db
from ..util import now


async def audit(actor: str, action: str, target: str = "", detail: dict | None = None):
    await db.audit_logs.insert_one({"actor": actor, "action": action, "target": target, "detail": detail or {}, "at": now()})
