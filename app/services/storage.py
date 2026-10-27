"""Private image storage on Cloudinary. Every upload uses type=authenticated, so no public URL exists.
Images are only ever served through director-authenticated API proxy endpoints."""
import asyncio
import re
from uuid import uuid4

import cloudinary
import cloudinary.uploader
import cloudinary.utils
import httpx

from ..config import settings

cloudinary.config(cloud_name=settings.cloudinary_cloud_name, api_key=settings.cloudinary_api_key,
                  api_secret=settings.cloudinary_api_secret, secure=True)


def configured() -> bool:
    return bool(settings.cloudinary_cloud_name and settings.cloudinary_api_key and settings.cloudinary_api_secret)


async def upload_private(data: bytes, folder: str) -> str:
    if not configured():
        raise RuntimeError("Cloudinary is not configured")
    r = await asyncio.to_thread(cloudinary.uploader.upload, data, type="authenticated", folder=folder,
                                resource_type="image", overwrite=False, unique_filename=True)
    return r["public_id"]


async def fetch(public_id: str) -> bytes:
    url = cloudinary.utils.private_download_url(public_id, "jpg", resource_type="image", type="authenticated")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


async def upload_private_document(data: bytes, filename: str, folder: str) -> str:
    """Store evidence as authenticated raw content, never as a public asset."""
    if not configured():
        raise RuntimeError("Cloudinary is not configured")
    safe_name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else "bin"
    stem = safe_name.rsplit(".", 1)[0] if "." in safe_name else safe_name
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:80] or "evidence"
    result = await asyncio.to_thread(
        cloudinary.uploader.upload, data, type="authenticated", folder=folder,
        resource_type="raw", public_id=f"{stem}-{uuid4().hex}.{extension}",
        overwrite=False,
    )
    return result["public_id"]


async def fetch_private_document(public_id: str, mime_type: str) -> bytes:
    if not configured():
        raise RuntimeError("Cloudinary is not configured")
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime_type)
    if not extension:
        raise ValueError("Unsupported private document format")
    url = cloudinary.utils.private_download_url(public_id, extension, resource_type="raw", type="authenticated")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def delete(public_id: str):
    await asyncio.to_thread(cloudinary.uploader.destroy, public_id, type="authenticated", invalidate=True)


async def delete_private_document(public_id: str):
    await asyncio.to_thread(cloudinary.uploader.destroy, public_id, type="authenticated", resource_type="raw", invalidate=True)
