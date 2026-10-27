"""Face matching providers. Local SFace matching is the default; cloud matching is optional."""
import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import settings


@dataclass
class FaceOutcome:
    provider: str
    score: float | None
    quality_ok: bool
    liveness_ok: bool | None
    reason: str
    configured: bool = True


class NoProvider:
    name = "none"
    available = False

    def __init__(self, reason="Automatic face matching is disabled. Contact the system administrator."):
        self.unavailable_reason = reason

    async def check_quality(self, img: bytes) -> tuple[bool, str]:
        return True, "Automatic face check unavailable; manual review required"

    async def liveness(self, frontal: bytes, turned: bytes):
        return None, "Liveness check is not enabled"

    async def compare(self, refs: list[bytes], live: bytes) -> FaceOutcome:
        return FaceOutcome(self.name, None, True, None, "Face matching is not configured", configured=False)


class OpenCVSFaceProvider:
    """On-device style matching performed locally on the application server."""
    name = "opencv_sface"
    match_threshold = settings.local_face_match_threshold

    def __init__(self):
        import cv2

        self.cv2 = cv2
        self.available = False
        self.inference_lock = threading.Lock()
        model_dir = Path(__file__).resolve().parent.parent / "models"
        detector = model_dir / "face_detection_yunet_2023mar.onnx"
        recognizer = model_dir / "face_recognition_sface_2021dec.onnx"
        if not detector.is_file() or not recognizer.is_file():
            self.unavailable_reason = "Face recognition files are not installed on the server. Contact the system administrator."
            return
        self.detector = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), 0.9, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
        cv2.setNumThreads(1)
        self.available = True
        self.unavailable_reason = ""

    def _decode_and_detect(self, data: bytes):
        import numpy as np

        image = self.cv2.imdecode(np.frombuffer(data, dtype=np.uint8), self.cv2.IMREAD_COLOR)
        if image is None:
            return None, None, "The photo could not be read"
        h, w = image.shape[:2]
        self.detector.setInputSize((w, h))
        _, faces = self.detector.detect(image)
        if faces is None or len(faces) == 0:
            return image, None, "No face was found. Retake the photo facing the camera."
        if len(faces) != 1:
            return image, None, "More than one face was found. Retake the photo alone."
        face = faces[0]
        if float(face[-1]) < 0.90 or min(float(face[2]), float(face[3])) < 80:
            return image, None, "Move closer and make sure your face is clear and well lit."
        return image, face, "ok"

    def _feature(self, data: bytes):
        image, face, why = self._decode_and_detect(data)
        if face is None:
            return None, why
        aligned = self.recognizer.alignCrop(image, face)
        return self.recognizer.feature(aligned), "ok"

    def _compare(self, refs: list[bytes], live: bytes):
        query, why = self._feature(live)
        if query is None:
            return None, why
        scores = []
        for ref in refs:
            feature, reason = self._feature(ref)
            if feature is None:
                return None, f"An enrolment photo could not be checked: {reason}"
            scores.append(float(self.recognizer.match(feature, query, self.cv2.FaceRecognizerSF_FR_COSINE)))
        if not scores:
            return None, "No enrolment photos are available"
        return max(scores), "compared"

    def _compare_locked(self, refs: list[bytes], live: bytes):
        with self.inference_lock:
            return self._compare(refs, live)

    async def check_quality(self, img: bytes):
        if not self.available:
            return True, "Automatic face check unavailable; manual review required"
        try:
            def detect_safely():
                with self.inference_lock:
                    return self._decode_and_detect(img)
            _, face, why = await asyncio.to_thread(detect_safely)
            return face is not None, why
        except Exception:
            return True, "Automatic face check unavailable; manual review required"

    async def liveness(self, frontal: bytes, turned: bytes):
        # Face matching alone cannot establish that a camera feed is live.
        return None, "Liveness is not configured for local face matching"

    async def compare(self, refs: list[bytes], live: bytes) -> FaceOutcome:
        if not self.available:
            return FaceOutcome(self.name, None, True, None, self.unavailable_reason, configured=False)
        try:
            score, why = await asyncio.to_thread(self._compare_locked, refs, live)
        except Exception as e:
            return FaceOutcome(self.name, None, True, None, f"Local face matcher error: {type(e).__name__}", configured=False)
        if score is None:
            return FaceOutcome(self.name, None, False, None, why)
        return FaceOutcome(self.name, score, True, None, "Face comparison completed")


class RekognitionProvider:
    name = "rekognition"

    def __init__(self):
        import boto3
        self.c = boto3.client("rekognition", region_name=settings.aws_region)
        self.available = True

    def _detect(self, img: bytes):
        r = self.c.detect_faces(Image={"Bytes": img}, Attributes=["ALL"])
        faces = r["FaceDetails"]
        if len(faces) != 1:
            return False, f"Expected exactly one face, found {len(faces)}"
        f = faces[0]
        if f["Confidence"] < 90:
            return False, "Face not clear"
        q = f["Quality"]
        if q["Brightness"] < 30 or q["Sharpness"] < 30:
            return False, "Too dark or blurry"
        if abs(f["Pose"]["Yaw"]) > 35 or abs(f["Pose"]["Pitch"]) > 30:
            return False, "Face turned too far from camera"
        if f["Sunglasses"]["Value"] and f["Sunglasses"]["Confidence"] > 90:
            return False, "Remove sunglasses"
        return True, "ok"

    async def check_quality(self, img: bytes):
        try:
            return await asyncio.to_thread(self._detect, img)
        except Exception:
            return True, "Automatic face check unavailable; manual review required"

    def _compare(self, ref: bytes, live: bytes):
        r = self.c.compare_faces(SourceImage={"Bytes": ref}, TargetImage={"Bytes": live}, SimilarityThreshold=0)
        return max((x["Similarity"] for x in r["FaceMatches"]), default=0.0)

    def _liveness(self, a: bytes, b: bytes):
        fa = self.c.detect_faces(Image={"Bytes": a}, Attributes=["ALL"])["FaceDetails"]
        fb = self.c.detect_faces(Image={"Bytes": b}, Attributes=["ALL"])["FaceDetails"]
        if len(fa) != 1 or len(fb) != 1:
            return False, "Exactly one face must be visible in both photos"
        if abs(fa[0]["Pose"]["Yaw"]) > 20:
            return False, "First photo must face the camera"
        if abs(fb[0]["Pose"]["Yaw"]) < 20:
            return False, "Second photo: turn your head to one side"
        score = self._compare(a, b)
        if score < settings.liveness_pair_threshold:
            return False, "The two photos do not appear to show the same person"
        return True, "ok"

    async def liveness(self, frontal: bytes, turned: bytes):
        try:
            return await asyncio.to_thread(self._liveness, frontal, turned)
        except Exception as e:
            return None, f"Liveness provider error: {type(e).__name__}"

    async def compare(self, refs: list[bytes], live: bytes) -> FaceOutcome:
        try:
            ok, why = await asyncio.to_thread(self._detect, live)
            if not ok:
                return FaceOutcome(self.name, None, False, None, why)
            scores = [await asyncio.to_thread(self._compare, ref, live) for ref in refs]
        except Exception as e:
            return FaceOutcome(self.name, None, True, None, f"Provider error: {type(e).__name__}", configured=False)
        return FaceOutcome(self.name, max(scores) if scores else None, True, None, "Face comparison completed")


_provider = None


def get_provider():
    global _provider
    if _provider is None:
        try:
            _provider = RekognitionProvider() if settings.face_provider == "rekognition" else (
                OpenCVSFaceProvider() if settings.face_provider == "opencv" else NoProvider()
            )
        except Exception:
            _provider = NoProvider("The face matcher could not start. Contact the system administrator.")
    return _provider
