import asyncio
import smtplib
from email.message import EmailMessage

from ..config import settings
from ..db import db
from ..util import now
from . import push as pushsvc

DIRECTOR = "DIRECTOR"


def _send_email(to: str, subject: str, body: str):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)


async def notify(recipient: str, stream_id, title: str, body: str, kind: str, dedupe_key: str | None = None):
    """recipient = call-up number or 'DIRECTOR'. Titles must stay generic (lock-screen safe)."""
    doc = {"recipient": recipient, "stream_id": stream_id, "title": title, "body": body, "kind": kind,
           "read": False, "created_at": now(), "delivery": [{"channel": "in_app", "ok": True, "at": now()}]}
    if dedupe_key:
        doc["dedupe_key"] = dedupe_key
        if await db.notifications.find_one({"dedupe_key": dedupe_key}, {"_id": 1}):
            return None
    res = await db.notifications.insert_one(doc)
    if settings.smtp_host and recipient != DIRECTOR:
        m = await db.members.find_one({"callup": recipient}, {"email": 1})
        if m and m.get("email"):
            try:
                await asyncio.to_thread(_send_email, m["email"], title, body)
                ok = True
            except Exception:
                ok = False
            await db.notifications.update_one({"_id": res.inserted_id}, {"$push": {"delivery": {"channel": "email", "ok": ok, "at": now()}}})
            if not ok and kind != "delivery_failure":
                await notify(DIRECTOR, stream_id, "Notification delivery failed", "An email notification could not be delivered.", "delivery_failure", dedupe_key=f"delfail:{res.inserted_id}")
    pr = await pushsvc.push(recipient, title)
    if pr is not None:
        await db.notifications.update_one({"_id": res.inserted_id}, {"$push": {"delivery": {"channel": "push", "ok": pr, "at": now()}}})
    return res.inserted_id
