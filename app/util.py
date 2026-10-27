from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException


def now():
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def oid(s: str) -> ObjectId:
    try:
        return ObjectId(s)
    except Exception:
        raise HTTPException(400, "Invalid id")


HIDDEN = {"password_hash", "enrollment", "selfie_public_id"}


def ser(d):
    """Mongo doc -> JSON-able; strips secrets and biometric storage references."""
    if isinstance(d, list):
        return [ser(x) for x in d]
    if isinstance(d, dict):
        return {("id" if k == "_id" else k): ser(v) for k, v in d.items() if k not in HIDDEN}
    if isinstance(d, ObjectId):
        return str(d)
    if isinstance(d, datetime):
        return d.isoformat()
    return d
