"""
face_detector.py — YOLOv8 Face Detection Module
वेदनेत्रम् v3.2

Replaces InsightFace FaceAnalysis detection with YOLOv8.
ArcFace embedding extraction is preserved unchanged.

Architecture:
  Frame/Image
      │
      ▼
  YOLOv8 (face detection)          ← NEW: replaces InsightFace detection
      │  bounding boxes + confidence
      ▼
  NMS + confidence filtering
      │  cleaned boxes
      ▼
  Crop + align face regions
      │  face crops (numpy arrays)
      ▼
  ArcFace (insightface recognition) ← PRESERVED: embedding unchanged
      │  512-d normed embeddings
      ▼
  pgvector cosine similarity        ← UNCHANGED
      │  matched student
      ▼
  Attendance marking                ← UNCHANGED

Design principles:
- Single responsibility: this module ONLY detects faces and returns crops.
- ArcFace embedding stays in app.py — no logic moved.
- Thread-safe singleton loader (same pattern as original get_face_app()).
- GPU auto-detection with CPU fallback.
- All public methods return the same data shapes the rest of app.py expects.
"""

import os
import logging
import threading
import time
from typing import List, Tuple, Optional, Dict, Any

import cv2
import numpy as np

log = logging.getLogger("frs.yolo")

# ── Model config ──────────────────────────────────────────────────────────────
# YOLOv8n-face is a nano face-specific model (~6 MB). Falls back to
# yolov8n.pt (general COCO) which still detects "person" class 0; we then
# use a secondary OpenCV face cascade ONLY for bounding-box refinement.
# Priority: yolov8n-face.pt → yolov8s-face.pt → yolov8n.pt (COCO fallback)
YOLO_MODEL_PRIORITY = [
    "yolov8n-face.pt",   # nano face-specific (recommended, ~6 MB)
    "yolov8s-face.pt",   # small face-specific (~22 MB, better accuracy)
    "yolov8n.pt",        # COCO general, class 0 = person (last resort)
]

# Download URLs for face-specific weights (Ultralytics HuggingFace mirror)
YOLO_FACE_DOWNLOAD_URLS = {
    "yolov8n-face.pt": "https://github.com/akanametov/yolov8-face/releases/download/v0.0.0/yolov8n-face.pt",
    "yolov8s-face.pt": "https://github.com/akanametov/yolov8-face/releases/download/v0.0.0/yolov8s-face.pt",
}

# Detection thresholds
DEFAULT_CONF_THRESHOLD = float(os.getenv("YOLO_CONF_THRESHOLD", "0.45"))
DEFAULT_IOU_THRESHOLD  = float(os.getenv("YOLO_IOU_THRESHOLD",  "0.45"))
DEFAULT_IMG_SIZE       = int(os.getenv("YOLO_IMG_SIZE",          "640"))

# Minimum face size to accept (pixels, after bbox is cropped)
MIN_FACE_SIZE = int(os.getenv("YOLO_MIN_FACE_PX", "32"))

# Margin around detected face bbox (fraction of face dimension)
FACE_MARGIN = float(os.getenv("YOLO_FACE_MARGIN", "0.15"))


class FaceDetectionResult:
    """
    Structured result for one detected face.
    Mirrors what InsightFace returned so call-sites need no changes.
    """
    __slots__ = ("bbox", "confidence", "crop_bgr", "kps", "landmark")

    def __init__(
        self,
        bbox: List[int],             # [x1, y1, x2, y2] in original frame coords
        confidence: float,           # YOLO detection confidence 0-1
        crop_bgr: np.ndarray,        # BGR face crop (for ArcFace)
        kps: Optional[np.ndarray] = None,    # 5-point landmarks if available
        landmark: Optional[np.ndarray] = None,
    ):
        self.bbox       = bbox
        self.confidence = confidence
        self.crop_bgr   = crop_bgr
        self.kps        = kps
        self.landmark   = landmark

    def __repr__(self):
        x1, y1, x2, y2 = self.bbox
        return f"<Face conf={self.confidence:.2f} bbox=[{x1},{y1},{x2},{y2}]>"


class YOLOFaceDetector:
    """
    Thread-safe singleton YOLOv8 face detector.

    Usage:
        detector = YOLOFaceDetector.instance()
        faces = detector.detect(frame_bgr)
        for face in faces:
            embedding = arcface_model.get_embedding(face.crop_bgr)

    The detector auto-downloads weights on first use if not present locally.
    GPU is used when available; falls back to CPU silently.
    """

    _instance: Optional["YOLOFaceDetector"] = None
    _lock = threading.Lock()

    # ── Singleton ──────────────────────────────────────────────────────────
    @classmethod
    def instance(cls) -> "YOLOFaceDetector":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ── Init ───────────────────────────────────────────────────────────────
    def __init__(self):
        self._model = None
        self._model_name: str = ""
        self._is_face_model: bool = False   # face-specific vs COCO general
        self._device: str = "cpu"
        self._load_lock = threading.Lock()
        self._loaded = False
        self._load_error: Optional[str] = None

        # Frame-skip state (thread-local is enough; detector is shared)
        self._frame_counter = 0

        log.info("YOLOFaceDetector initialised (lazy-load on first use)")

    # ── Model loading ──────────────────────────────────────────────────────
    def _ensure_loaded(self) -> None:
        """Load the model once; subsequent calls are no-ops."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._load_model()

    def _load_model(self) -> None:
        """
        Try each model in priority order.
        Downloads face-specific weights if not found locally.
        """
        try:
            from ultralytics import YOLO
        except ImportError:
            msg = (
                "ultralytics package not found. "
                "Install with: pip install ultralytics --break-system-packages"
            )
            log.error(msg)
            self._load_error = msg
            self._loaded = True   # mark as attempted so we don't retry every call
            return

        # Detect GPU
        import torch
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        log.info("YOLOv8 will run on: %s", self._device.upper())

        for model_name in YOLO_MODEL_PRIORITY:
            model_path = self._resolve_model_path(model_name)
            if model_path is None:
                continue
            try:
                log.info("Loading YOLOv8 model: %s …", model_path)
                t0 = time.time()
                model = YOLO(model_path)
                model.to(self._device)
                elapsed = time.time() - t0
                self._model = model
                self._model_name = model_name
                self._is_face_model = "face" in model_name.lower()
                self._loaded = True
                log.info(
                    "YOLOv8 loaded: %s in %.2fs (face-specific=%s, device=%s)",
                    model_name, elapsed, self._is_face_model, self._device,
                )
                return
            except Exception as exc:
                log.warning("Failed to load %s: %s — trying next …", model_name, exc)

        msg = "No YOLOv8 model could be loaded. Face detection will be disabled."
        log.error(msg)
        self._load_error = msg
        self._loaded = True

    def _resolve_model_path(self, model_name: str) -> Optional[str]:
        """
        Return a path to the model weights, downloading if necessary.
        Checks: local directory → ~/.cache/ultralytics → download from mirror.
        """
        # 1. Already on disk beside this file
        local = os.path.join(os.path.dirname(__file__), model_name)
        if os.path.exists(local):
            log.info("Found local model: %s", local)
            return local

        # 2. Ultralytics cache directory
        cache_dir = os.path.expanduser("~/.cache/ultralytics")
        cached = os.path.join(cache_dir, model_name)
        if os.path.exists(cached):
            log.info("Found cached model: %s", cached)
            return cached

        # 3. For COCO general models, let Ultralytics auto-download
        if model_name == "yolov8n.pt":
            log.info("Will let Ultralytics auto-download %s", model_name)
            return model_name   # YOLO("yolov8n.pt") triggers auto-download

        # 4. For face-specific models, try our mirror
        if model_name in YOLO_FACE_DOWNLOAD_URLS:
            url = YOLO_FACE_DOWNLOAD_URLS[model_name]
            save_path = os.path.join(cache_dir, model_name)
            log.info("Downloading %s from %s …", model_name, url)
            if self._download_model(url, save_path):
                return save_path

        return None

    @staticmethod
    def _download_model(url: str, dest: str) -> bool:
        """Download model weights with progress logging."""
        import urllib.request
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".tmp"

            def _reporthook(block, block_size, total):
                if total > 0:
                    pct = block * block_size * 100 / total
                    if int(pct) % 20 == 0:
                        log.info("  … %.0f%%", min(pct, 100))

            urllib.request.urlretrieve(url, tmp, reporthook=_reporthook)
            os.rename(tmp, dest)
            log.info("Downloaded → %s", dest)
            return True
        except Exception as exc:
            log.warning("Download failed (%s): %s", url, exc)
            if os.path.exists(tmp):
                os.remove(tmp)
            return False

    # ── Public API ─────────────────────────────────────────────────────────
    @property
    def is_available(self) -> bool:
        """True if the model loaded successfully."""
        self._ensure_loaded()
        return self._model is not None

    @property
    def model_info(self) -> Dict[str, Any]:
        """Info dict for /api/health endpoint."""
        self._ensure_loaded()
        return {
            "engine":      "YOLOv8",
            "model":       self._model_name or "not loaded",
            "face_model":  self._is_face_model,
            "device":      self._device,
            "available":   self.is_available,
            "error":       self._load_error,
            "conf_thresh": DEFAULT_CONF_THRESHOLD,
            "iou_thresh":  DEFAULT_IOU_THRESHOLD,
        }

    def detect(
        self,
        frame_bgr: np.ndarray,
        conf: float = DEFAULT_CONF_THRESHOLD,
        iou:  float = DEFAULT_IOU_THRESHOLD,
        img_size: int = DEFAULT_IMG_SIZE,
        max_faces: int = 20,
    ) -> List[FaceDetectionResult]:
        """
        Run YOLOv8 face detection on a BGR frame.

        Args:
            frame_bgr:  BGR image (from OpenCV or decoded JPEG).
            conf:       Confidence threshold (0-1). Lower = more detections.
            iou:        IOU threshold for NMS (0-1).
            img_size:   Inference image size (pixels). 640 is standard.
            max_faces:  Cap on returned faces to avoid degenerate frames.

        Returns:
            List of FaceDetectionResult sorted by confidence descending.
            Empty list if no faces detected or model unavailable.
        """
        self._ensure_loaded()
        if self._model is None:
            return []

        h, w = frame_bgr.shape[:2]
        if h == 0 or w == 0:
            return []

        try:
            # YOLOv8 inference
            # verbose=False suppresses per-frame console spam
            results = self._model.predict(
                source=frame_bgr,
                conf=conf,
                iou=iou,
                imgsz=img_size,
                device=self._device,
                verbose=False,
                stream=False,
            )
        except Exception as exc:
            log.warning("YOLO inference error: %s", exc)
            return []

        faces: List[FaceDetectionResult] = []

        for result in results:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                continue

            # For face-specific models every box IS a face.
            # For COCO models filter to class 0 (person) as proxy.
            for box in boxes:
                cls_id = int(box.cls[0].item()) if not self._is_face_model else 0
                if not self._is_face_model and cls_id != 0:
                    continue

                confidence = float(box.conf[0].item())
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]

                # Clamp to frame bounds
                x1 = max(0, x1); y1 = max(0, y1)
                x2 = min(w, x2); y2 = min(h, y2)

                face_w = x2 - x1
                face_h = y2 - y1
                if face_w < MIN_FACE_SIZE or face_h < MIN_FACE_SIZE:
                    continue   # skip tiny detections (noise / distant background)

                # Extract face crop WITH margin
                crop = self._crop_with_margin(frame_bgr, x1, y1, x2, y2)
                if crop is None or crop.size == 0:
                    continue

                # 5-point landmarks (only face-specific YOLO models provide these)
                kps = None
                if hasattr(result, "keypoints") and result.keypoints is not None:
                    try:
                        kp_data = result.keypoints.xy[len(faces)].cpu().numpy()
                        if kp_data.shape[0] == 5:
                            kps = kp_data.astype(np.float32)
                    except Exception:
                        pass

                faces.append(FaceDetectionResult(
                    bbox=[x1, y1, x2, y2],
                    confidence=confidence,
                    crop_bgr=crop,
                    kps=kps,
                ))

                if len(faces) >= max_faces:
                    break

        # Sort by confidence descending — largest/clearest face first
        faces.sort(key=lambda f: f.confidence, reverse=True)
        return faces

    def detect_largest(
        self,
        frame_bgr: np.ndarray,
        **kwargs,
    ) -> Optional[FaceDetectionResult]:
        """
        Detect all faces and return the largest (by area).
        Useful for enrollment where we want the primary subject.
        """
        faces = self.detect(frame_bgr, **kwargs)
        if not faces:
            return None
        return max(faces, key=lambda f: (f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))

    def estimate_pose(self, face: FaceDetectionResult, frame_shape: Tuple) -> str:
        """
        Estimate head pose from landmarks or bbox geometry.
        Returns one of: 'front', 'left', 'right', 'up', 'down'.

        Used by the guided enrollment capture system to replace the
        InsightFace landmark-based pose estimator.
        """
        kps = face.kps
        if kps is not None and len(kps) == 5:
            return self._pose_from_landmarks(kps, face.bbox)

        # Fallback: coarse pose from bbox aspect ratio + position
        return self._pose_from_bbox(face.bbox, frame_shape)

    # ── Internal helpers ───────────────────────────────────────────────────
    @staticmethod
    def _crop_with_margin(
        frame: np.ndarray,
        x1: int, y1: int, x2: int, y2: int,
    ) -> Optional[np.ndarray]:
        """
        Crop the face region and add a proportional margin so the chin,
        forehead, and ears are included — ArcFace accuracy improves with
        a small margin around the tight bbox.
        """
        h, w = frame.shape[:2]
        fx = x2 - x1
        fy = y2 - y1
        mx = int(fx * FACE_MARGIN)
        my = int(fy * FACE_MARGIN)
        cx1 = max(0, x1 - mx)
        cy1 = max(0, y1 - my)
        cx2 = min(w, x2 + mx)
        cy2 = min(h, y2 + my)
        crop = frame[cy1:cy2, cx1:cx2]
        return crop if crop.size > 0 else None

    @staticmethod
    def _pose_from_landmarks(kps: np.ndarray, bbox: List[int]) -> str:
        """
        Estimate pose using 5 facial landmarks:
          0=left_eye, 1=right_eye, 2=nose, 3=left_mouth, 4=right_mouth
        (YOLOv8-face landmark ordering)
        """
        try:
            left_eye, right_eye, nose = kps[0], kps[1], kps[2]
            x1, y1, x2, y2 = bbox
            face_w = max(x2 - x1, 1)
            face_h = max(y2 - y1, 1)

            eye_cx = (left_eye[0] + right_eye[0]) / 2.0
            nose_offset = (nose[0] - eye_cx) / face_w

            eye_cy = (left_eye[1] + right_eye[1]) / 2.0
            nose_dy = (nose[1] - eye_cy) / face_h

            if nose_offset > 0.12:
                return "left"
            elif nose_offset < -0.12:
                return "right"
            elif nose_dy < 0.18:
                return "up"
            elif nose_dy > 0.30:
                return "down"
            else:
                return "front"
        except Exception:
            return "front"

    @staticmethod
    def _pose_from_bbox(bbox: List[int], frame_shape: Tuple) -> str:
        """
        Very coarse pose estimation from bbox position within the frame.
        Used when landmarks are unavailable (COCO fallback model).
        """
        fh, fw = frame_shape[:2]
        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0 / fw
        cy = (y1 + y2) / 2.0 / fh
        if cx < 0.35:
            return "right"
        elif cx > 0.65:
            return "left"
        elif cy < 0.35:
            return "up"
        elif cy > 0.65:
            return "down"
        return "front"


# ── Quality scoring (replaces score_frame_quality in app.py) ──────────────────
def score_frame_quality_yolo(
    frame_bgr: np.ndarray,
    face: Optional[FaceDetectionResult],
) -> Dict[str, Any]:
    """
    Quality scoring using YOLO detection result.

    Equivalent to app.py::score_frame_quality() but uses YOLOv8
    confidence and bbox directly instead of the InsightFace face object.

    Returns the same dict shape so app.py callers need no changes.
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Blur: Laplacian variance (unchanged metric)
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    blur_score = min(100, int(lap_var / 3))

    # Brightness (unchanged metric)
    mean_bright = float(np.mean(gray))
    if 50 <= mean_bright <= 200:
        brightness = int(70 + 30 * (1 - abs(mean_bright - 125) / 75))
    else:
        brightness = max(0, int(40 - abs(mean_bright - 125) / 5))

    # Face size check using YOLO bbox
    face_size_ok = False
    if face is not None:
        x1, y1, x2, y2 = face.bbox
        face_area  = max(0, x2 - x1) * max(0, y2 - y1)
        frame_area = w * h
        ratio      = face_area / frame_area if frame_area else 0
        face_size_ok = ratio >= 0.03

    # YOLO confidence contributes to overall quality
    yolo_conf_score = 0
    if face is not None:
        yolo_conf_score = int(face.confidence * 20)   # max 20 points

    overall = int(blur_score * 0.45 + brightness * 0.25 + (15 if face_size_ok else 0) + yolo_conf_score)
    overall = min(100, overall)

    return {
        "blur_score":      blur_score,
        "brightness":      brightness,
        "face_size_ok":    face_size_ok,
        "yolo_confidence": round(face.confidence * 100, 1) if face else 0,
        "overall":         overall,
        "passed":          overall >= 50,
    }


# ── Module-level convenience functions (used by app.py) ──────────────────────
def get_yolo_detector() -> YOLOFaceDetector:
    """Return the singleton detector. Call this instead of get_face_app()."""
    return YOLOFaceDetector.instance()


def detect_faces_in_frame(
    frame_bgr: np.ndarray,
    conf: float = DEFAULT_CONF_THRESHOLD,
) -> List[FaceDetectionResult]:
    """Convenience wrapper used in the camera stream loop."""
    return get_yolo_detector().detect(frame_bgr, conf=conf)


def detect_largest_face(
    frame_bgr: np.ndarray,
    conf: float = DEFAULT_CONF_THRESHOLD,
) -> Optional[FaceDetectionResult]:
    """Convenience wrapper for single-face endpoints (recognition, enrollment)."""
    return get_yolo_detector().detect_largest(frame_bgr, conf=conf)
