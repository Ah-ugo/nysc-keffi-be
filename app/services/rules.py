"""Pure business rules (no I/O) so they can be unit-tested without MongoDB."""
import io
import math
import re
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageStat

_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise spreadsheet formula injection."""
    if value is None:
        return ""
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIX) else s


def normalise_callup(raw: str) -> str:
    return re.sub(r"\s+", "", raw or "").upper()


def valid_callup(callup: str, pattern: str) -> bool:
    return bool(re.match(pattern, callup))


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class LocationResult:
    result: str  # passed | failed | review
    distance_m: float | None
    reason: str


def evaluate_location(lat, lon, accuracy, cfg) -> LocationResult:
    """Called ONLY after face verification passed."""
    if cfg.get("configured") is False or (cfg.get("lat") == 0 and cfg.get("lon") == 0):
        return LocationResult("review", None, "Camp location has not been set by the Camp Director")
    if lat is None or lon is None or accuracy is None:
        return LocationResult("review", None, "Location missing")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return LocationResult("failed", None, "Invalid coordinates")
    d = haversine_m(lat, lon, cfg["lat"], cfg["lon"])
    if accuracy > cfg["max_accuracy_m"]:
        return LocationResult("review", d, f"GPS accuracy {accuracy:.0f}m exceeds {cfg['max_accuracy_m']:.0f}m limit")
    if d > cfg["radius_m"]:
        return LocationResult("failed", d, f"{d:.0f}m from camp centre; radius {cfg['radius_m']:.0f}m")
    return LocationResult("passed", d, "Inside geofence")


def decide_face(score, quality_ok, liveness_ok, match_th, review_th):
    """-> (result, reason). result in passed|failed|review."""
    if not quality_ok:
        return "review", "Image quality unusable"
    if liveness_ok is False:
        return "review", "Liveness check failed"
    if score is None:
        return "review", "No score returned"
    if score >= match_th:
        return "passed", f"Match {score:.1f} >= {match_th}"
    if score >= review_th:
        return "review", f"Inconclusive match {score:.1f}"
    return "failed", f"Match {score:.1f} < {review_th}"


def final_status(face: str, loc: str) -> str:
    if face == "failed":
        return "face_failed"
    if face == "review":
        return "pending_manual_review"
    if loc == "failed":
        return "location_failed"
    if loc == "review":
        return "pending_manual_review"
    return "verified_present"


def image_quality(data: bytes, min_side=480):
    """Cheap pre-checks: decodable, size, brightness, sharpness. Returns (ok, reason)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return False, "Not a valid image"
    if min(img.size) < min_side:
        return False, "Image resolution too low"
    g = img.convert("L").resize((320, 320))
    mean = ImageStat.Stat(g).mean[0]
    if mean < 50:
        return False, "Too dark - move to better light"
    if mean > 215:
        return False, "Too bright - avoid strong light behind or on the face"
    if ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0] < 40:
        return False, "Image is blurry - hold the phone steady"
    return True, "ok"
