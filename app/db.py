from motor.motor_asyncio import AsyncIOMotorClient
from .config import settings

client = AsyncIOMotorClient(
    settings.mongo_uri,
    tz_aware=True,
    # Fail promptly when Atlas is unreachable instead of holding each API
    # request open for PyMongo's 30-second default.
    serverSelectionTimeoutMS=5_000,
    connectTimeoutMS=5_000,
)
db = client[settings.mongo_db]


async def ensure_indexes():
    await db.users.create_index("username", unique=True)
    await db.streams.create_index([("year", 1), ("batch", 1), ("number", 1)], unique=True)
    await db.members.create_index("callup", unique=True)
    await db.members.create_index([("stream_id", 1), ("full_name", 1)])
    await db.hostels.create_index([("stream_id", 1), ("code", 1)], unique=True)
    await db.beds.create_index([("stream_id", 1), ("code", 1)], unique=True)
    await db.beds.create_index([("stream_id", 1), ("hostel_code", 1), ("status", 1)])
    active = {"partialFilterExpression": {"status": "active"}, "unique": True}
    await db.assignments.create_index([("stream_id", 1), ("bed_code", 1)], **active)
    await db.assignments.create_index([("stream_id", 1), ("callup", 1)], **active)
    await db.attendance_schedules.create_index([("stream_id", 1), ("week", 1)], unique=True)
    await db.attendance.create_index([("schedule_id", 1), ("callup", 1)], unique=True)
    await db.notifications.create_index("dedupe_key", unique=True, sparse=True)
    await db.notifications.create_index([("recipient", 1), ("created_at", -1)])
    await db.exits.create_index([("stream_id", 1), ("callup", 1), ("status", 1)])
    await db.exit_request_locks.create_index([("stream_id", 1), ("callup", 1)], unique=True)
    await db.exit_categories.create_index([("stream_id", 1), ("name", 1)], unique=True)
    await db.platoons.create_index([("stream_id", 1), ("number", 1)], unique=True)
    await db.platoon_configs.create_index("stream_id", unique=True)
    await db.platoon_assignments.create_index([("stream_id", 1), ("callup", 1), ("status", 1)])
    await db.platoon_assignments.create_index([("stream_id", 1), ("callup", 1)], unique=True,
                                              partialFilterExpression={"status": "active"})
    await db.platoon_assignments.create_index([("stream_id", 1), ("platoon_number", 1), ("status", 1)])
    await db.swaps.create_index([("stream_id", 1), ("status", 1)])
    await db.audit_logs.create_index([("at", -1)])
    await db.liveness_nonces.create_index("nonce", unique=True)
    await db.liveness_nonces.create_index("at", expireAfterSeconds=900)
    await db.push_subs.create_index("subscription.endpoint", unique=True)
