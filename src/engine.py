"""Offline face redaction. All models, media and encoder inputs are local files.

Detection runs on every video frame. KLT optical flow predicts masks between
missed detections; identity exclusions are re-confirmed on each detected frame.
The encoder receives raw BGR frames through stdin, never through a network URL.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import threading
import uuid
import warnings as python_warnings
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

# The decoder must also reject playlist/container references to network protocols.
LOCAL_VIDEO_FORMATS = "mov,matroska,avi,asf,mpeg,mpegts"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file,pipe|format_whitelist;" + LOCAL_VIDEO_FORMATS

import cv2
import numpy as np
from PIL import Image, ImageOps


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".m4v", ".wmv", ".webm", ".mpeg", ".mpg", ".mts", ".m2ts"})
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


class EngineError(RuntimeError):
    pass


class CancelledError(EngineError):
    pass


class _BufferSFace:
    """SFace with Python file I/O, which supports non-ASCII Windows model paths.

    Uses the 112px five-landmark similarity alignment and RGB blob convention of
    OpenCV FaceRecognizerSF (OpenCV 4.10 modules/objdetect/src/face_recognize.cpp).
    The transform is computed with a least-squares similarity fit using NumPy.
    """
    def __init__(self, model: Path):
        self.net = cv2.dnn.readNetFromONNX(np.frombuffer(model.read_bytes(), dtype=np.uint8))

    def alignCrop(self, frame, face):
        source = np.asarray(face, dtype=np.float64).ravel()[4:14].reshape(5, 2)
        destination = np.asarray([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                                  [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
        source_mean = source.mean(axis=0)
        destination_mean = destination.mean(axis=0)
        centered_source = source - source_mean
        centered_destination = destination - destination_mean
        covariance = centered_destination.T @ centered_source / len(source)
        left, values, right = np.linalg.svd(covariance)
        sign = np.ones(2)
        if np.linalg.det(covariance) < 0:
            sign[-1] = -1
        rotation = left @ np.diag(sign) @ right
        variance = float(np.sum(centered_source ** 2) / len(source))
        if variance < 1e-8:
            raise cv2.error("invalid face landmarks")
        scale = float(np.dot(values, sign) / variance)
        transform = np.empty((2, 3), dtype=np.float64)
        transform[:, :2] = scale * rotation
        transform[:, 2] = destination_mean - scale * (rotation @ source_mean)
        return cv2.warpAffine(frame, transform, (112, 112), flags=cv2.INTER_LINEAR)

    def feature(self, aligned):
        blob = cv2.dnn.blobFromImage(aligned, 1.0, (112, 112), (0, 0, 0), swapRB=True, crop=False)
        self.net.setInput(blob)
        return self.net.forward()


@dataclass
class Settings:
    effect: str = "mosaic"
    mode: str = "all"
    strength: int = 24
    padding: float = 0.28
    threshold: float = 0.60
    detection_size: int = 1280
    preserve_audio: bool = True


@dataclass
class MediaSelection:
    keep_faces: List = field(default_factory=list)
    manual_boxes: List = field(default_factory=list)
    reference_frame: Optional[np.ndarray] = field(default=None, repr=False)
    anchor_frame: int = 0


def _asset_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def _local_path(path) -> Path:
    raw = os.fspath(path)
    if "://" in raw or raw.startswith("\\\\") or raw.startswith("//"):
        raise EngineError("仅支持本机文件，不支持网络地址或网络共享。")
    candidate = Path(raw).expanduser()
    if os.name == "nt":
        import ctypes
        candidate_anchor = candidate.anchor or Path.cwd().anchor
        # Check mapped drives before resolve(), which may query their filesystem.
        if ctypes.windll.kernel32.GetDriveTypeW(str(candidate_anchor)) == 4:
            raise EngineError("仅支持本机磁盘，不支持映射的网络驱动器。")
    resolved = candidate.resolve()
    # resolve() also exposes junctions pointing at UNC locations.
    if str(resolved).startswith("\\\\") or str(resolved).startswith("//"):
        raise EngineError("仅支持本机磁盘，不支持网络共享。")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(resolved.anchor)) == 4:
            raise EngineError("仅支持本机磁盘，不支持映射的网络驱动器。")
    return resolved


def validate_local_path(path) -> Path:
    """Return a canonical local path; it need not exist yet (e.g. output folder)."""
    return _local_path(path)


def _local_file(path) -> Path:
    resolved = _local_path(path)
    if not resolved.is_file():
        raise EngineError("文件不存在：" + str(resolved))
    return resolved


def _checked_settings(settings: Settings) -> None:
    if settings.effect not in {"mosaic", "blur", "solid"}:
        raise EngineError("未知的遮挡效果。")
    if settings.mode not in {"all", "largest", "selected"}:
        raise EngineError("未知的人物保留模式。")
    if not (1 <= int(settings.strength) <= 160):
        raise EngineError("遮挡强度需要在 1 到 160 之间。")
    if not (0 <= float(settings.padding) <= 1.5):
        raise EngineError("遮挡扩边需要在 0 到 150% 之间。")
    if not (0.1 <= float(settings.threshold) <= 0.99):
        raise EngineError("检测阈值需要在 0.1 到 0.99 之间。")
    if not (320 <= int(settings.detection_size) <= 4096):
        raise EngineError("检测分辨率需要在 320 到 4096 之间。")


def _read_image(path: Path, metadata: Optional[Dict] = None) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(str(path), formats=["JPEG", "PNG", "BMP", "TIFF", "WEBP"]) as im:
                source_format = str(im.format or "")
                source_frame_count = int(getattr(im, "n_frames", 1))
                # Camera JPEGs may use MPO to carry an auxiliary image (e.g.
                # a smaller preview or HDR data). Process the primary JPEG only;
                # this exception does not allow animation or multi-page images.
                if source_frame_count > 1 and source_format != "MPO":
                    raise EngineError("不支持多帧图片；请先转换成视频或单帧图片。")
                if source_format == "MPO":
                    im.seek(0)
                if metadata is not None:
                    metadata.update(source_format=source_format, source_frame_count=source_frame_count,
                                    primary_only=source_format == "MPO" and source_frame_count > 1)
                im = ImageOps.exif_transpose(im)
                alpha = None
                if "A" in im.getbands() or "transparency" in im.info:
                    alpha = np.asarray(im.convert("RGBA").getchannel("A")).copy()
                return np.asarray(im.convert("RGB"))[:, :, ::-1].copy(), alpha
    except EngineError:
        raise
    except Exception as exc:
        raise EngineError("无法读取图片：" + str(exc)) from exc


def _capture(path: Path):
    capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
    if not capture.isOpened():
        capture.release()
        raise EngineError("无法读取视频；请检查视频是否损坏或格式是否受支持。")
    if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
        capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 1)
    return capture


def read_preview(path, frame_index: int = 0) -> Tuple[np.ndarray, Dict]:
    path = _local_file(path)
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        metadata = {}
        frame, _ = _read_image(path, metadata)
        return frame, {"type": "image", "width": frame.shape[1], "height": frame.shape[0],
                       "fps": 0.0, "duration": 0.0, "frame_count": 1, "frame_index": 0, **metadata}
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise EngineError("不支持的文件类型：" + path.suffix)
    capture = _capture(path)
    try:
        count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        fps = fps if math.isfinite(fps) and fps > 0 else 25.0
        index = max(0, int(frame_index))
        if count:
            index = min(index, count - 1)
        if index:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise EngineError("无法解码预览帧。")
        return frame, {"type": "video", "width": frame.shape[1], "height": frame.shape[0],
                       "fps": fps, "duration": count / fps if count else 0.0,
                       "frame_count": count, "frame_index": index}
    finally:
        capture.release()


def _iou(a, b) -> float:
    x = max(float(a[0]), float(b[0]))
    y = max(float(a[1]), float(b[1]))
    x2 = min(float(a[0] + a[2]), float(b[0] + b[2]))
    y2 = min(float(a[1] + a[3]), float(b[1] + b[3]))
    intersection = max(0.0, x2 - x) * max(0.0, y2 - y)
    return intersection / max(1.0, float(a[2] * a[3] + b[2] * b[3] - intersection))


def _clip_box(box, width: int, height: int) -> Optional[np.ndarray]:
    if len(box) < 4 or not np.isfinite(np.asarray(box[:4], dtype=float)).all():
        return None
    x, y, w, h = map(float, box[:4])
    if w <= 0 or h <= 0:
        return None
    x1, y1 = max(0.0, x), max(0.0, y)
    x2, y2 = min(float(width), x + w), min(float(height), y + h)
    if x2 <= x1 or y2 <= y1:
        return None
    return np.asarray([x1, y1, x2 - x1, y2 - y1], dtype=np.float32)


def _apply_mask(frame: np.ndarray, box, settings: Settings, extra_padding: float = 0) -> None:
    height, width = frame.shape[:2]
    clipped = _clip_box(box, width, height)
    if clipped is None:
        return
    x, y, w, h = map(float, clipped)
    pad = float(settings.padding) + extra_padding
    x1 = max(0, int(math.floor(x - w * pad)))
    y1 = max(0, int(math.floor(y - h * pad)))
    x2 = min(width, int(math.ceil(x + w + w * pad)))
    y2 = min(height, int(math.ceil(y + h + h * pad)))
    region = frame[y1:y2, x1:x2]
    if not region.size:
        return
    if settings.effect == "solid":
        region[:] = 0
    elif settings.effect == "mosaic":
        # Limit to at most 12 cells across a face, even when a weak setting is used.
        block = max(int(settings.strength), int(math.ceil(max(w, h) / 12)), 4)
        tiny = cv2.resize(region, (max(1, region.shape[1] // block), max(1, region.shape[0] // block)), interpolation=cv2.INTER_AREA)
        region[:] = cv2.resize(tiny, (region.shape[1], region.shape[0]), interpolation=cv2.INTER_NEAREST)
    else:
        kernel = max(15, int(settings.strength) * 2 + 1, int(min(w, h) * 0.20) | 1)
        if kernel % 2 == 0:
            kernel += 1
        region[:] = cv2.GaussianBlur(region, (kernel, kernel), 0)


@dataclass
class _Track:
    box: np.ndarray
    points: Optional[np.ndarray] = None
    missing: int = 0
    kept: bool = False
    manual: bool = False
    low_quality: int = 0


class _VideoTracker:
    """Associates detections with short-lived forward/backward-checked KLT tracks."""
    def __init__(self, fps: float):
        self.previous = None
        self.tracks: List[_Track] = []
        self.manual: List[_Track] = []
        self.max_missing = max(8, min(30, int(fps * 0.7)))
        self.scene_cuts = 0
        self.manual_lost = 0
        self.uncertain_frames = 0
        self.manual_started = False

    @staticmethod
    def _points(gray, box):
        mask = np.zeros(gray.shape, dtype=np.uint8)
        clipped = _clip_box(box, gray.shape[1], gray.shape[0])
        if clipped is None:
            return None
        x, y, w, h = clipped.astype(int)
        mask[y:y + max(1, h), x:x + max(1, w)] = 255
        return cv2.goodFeaturesToTrack(gray, maxCorners=45, qualityLevel=0.015, minDistance=4, mask=mask)

    def _predict(self, track, gray):
        if track.points is None or len(track.points) < 4 or self.previous is None:
            track.low_quality += 1
            track.points = self._points(gray, track.box)
            return
        try:
            nxt, status, _ = cv2.calcOpticalFlowPyrLK(self.previous, gray, track.points, None,
                                                    winSize=(31, 31), maxLevel=3,
                                                    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 25, 0.02))
            if nxt is None or status is None:
                raise ValueError("flow unavailable")
            back, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.previous, nxt, None,
                                                         winSize=(31, 31), maxLevel=3)
            if back is None or back_status is None:
                raise ValueError("backward flow unavailable")
            good = (status.ravel() == 1) & (back_status.ravel() == 1)
            good &= np.linalg.norm(back.reshape(-1, 2) - track.points.reshape(-1, 2), axis=1) < 1.6
            old = track.points.reshape(-1, 2)[good]
            new = nxt.reshape(-1, 2)[good]
            if len(new) < 4:
                raise ValueError("too few flow points")
            displacement = np.median(new - old, axis=0)
            # Translation avoids a bad affine fit creating an unexpectedly tiny mask.
            track.box[:2] += displacement
            before_spread = np.median(np.linalg.norm(old - np.median(old, axis=0), axis=1))
            after_spread = np.median(np.linalg.norm(new - np.median(new, axis=0), axis=1))
            if before_spread > 4:
                scale = float(np.clip(after_spread / before_spread, 0.94, 1.08))
                center = track.box[:2] + track.box[2:4] / 2
                track.box[2:4] *= scale
                track.box[:2] = center - track.box[2:4] / 2
            track.points = new.reshape(-1, 1, 2).astype(np.float32)
            track.low_quality = 0
        except (cv2.error, ValueError):
            track.low_quality += 1
            track.points = self._points(gray, track.box)

    def _is_cut(self, gray):
        if self.previous is None:
            return False
        old = cv2.resize(self.previous, (96, 54))
        new = cv2.resize(gray, (96, 54))
        difference = float(np.mean(cv2.absdiff(old, new)))
        h1 = cv2.calcHist([old], [0], None, [32], [0, 256])
        h2 = cv2.calcHist([new], [0], None, [32], [0, 256])
        correlation = float(cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL))
        return difference > 52 and correlation < 0.55

    def update(self, frame, faces, kept: Set[int], manual_boxes, activate_manual: bool):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        cut = self._is_cut(gray)
        if cut:
            self.scene_cuts += 1
            self.tracks.clear()
            if self.manual:
                self.manual_lost += len(self.manual)
            self.manual.clear()
            self.previous = None
        for track in self.tracks + self.manual:
            self._predict(track, gray)
        if activate_manual and not self.manual_started:
            self.manual_started = True
            for box in manual_boxes:
                clipped = _clip_box(box, frame.shape[1], frame.shape[0])
                if clipped is not None:
                    self.manual.append(_Track(clipped, self._points(gray, clipped), manual=True))
        candidates = []
        for ti, track in enumerate(self.tracks):
            for fi, face in enumerate(faces):
                overlap = _iou(track.box, face)
                distance = np.linalg.norm(track.box[:2] + track.box[2:4] / 2 - face[:2] - face[2:4] / 2)
                scale = max(float(track.box[2]), float(track.box[3]), float(face[2]), float(face[3]), 1.0)
                if overlap > 0.1 or distance < scale * 0.55:
                    candidates.append((overlap - distance / scale * 0.12, ti, fi))
        matched_tracks, matched_faces = set(), set()
        for _, ti, fi in sorted(candidates, reverse=True):
            if ti in matched_tracks or fi in matched_faces:
                continue
            track = self.tracks[ti]
            track.box = np.asarray(faces[fi][:4], dtype=np.float32).copy()
            track.points = self._points(gray, track.box)
            track.missing = 0
            track.low_quality = 0
            track.kept = fi in kept
            matched_tracks.add(ti)
            matched_faces.add(fi)
        active = []
        for ti, track in enumerate(self.tracks):
            if ti not in matched_tracks:
                track.missing += 1
                # A face is only exempt while it was positively identified this frame.
                track.kept = False
            if track.missing <= self.max_missing and _clip_box(track.box, frame.shape[1], frame.shape[0]) is not None:
                active.append(track)
        for fi, face in enumerate(faces):
            if fi not in matched_faces:
                box = np.asarray(face[:4], dtype=np.float32).copy()
                active.append(_Track(box, self._points(gray, box), kept=fi in kept))
        self.tracks = active
        # Stop a manual track only after sustained flow failure; the report marks it for review.
        retained_manual = []
        for track in self.manual:
            if track.low_quality > self.max_missing:
                self.manual_lost += 1
            else:
                retained_manual.append(track)
        self.manual = retained_manual
        uncertain = any(t.missing or t.low_quality for t in self.tracks + self.manual)
        self.uncertain_frames += int(uncertain)
        self.previous = gray
        return self.tracks + self.manual


class FaceEngine:
    def __init__(self, model_dir=None, ffmpeg_path=None):
        model_dir = Path(model_dir) if model_dir else _asset_root() / "models"
        detection_model = model_dir / "face_detection_yunet_2023mar.onnx"
        if not detection_model.is_file():
            raise EngineError("缺少离线人脸检测模型：" + str(detection_model))
        try:
            self.detector = cv2.FaceDetectorYN.create("onnx", np.frombuffer(detection_model.read_bytes(), dtype=np.uint8),
                                                     np.empty(0, dtype=np.uint8), (320, 320), 0.6, 0.3, 5000)
        except cv2.error as exc:
            raise EngineError("无法加载离线检测模型：" + str(exc)) from exc
        recognition_model = model_dir / "face_recognition_sface_2021dec.onnx"
        self.recognizer = None
        if recognition_model.is_file():
            try:
                self.recognizer = _BufferSFace(recognition_model)
            except cv2.error:
                pass
        self.ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else _asset_root() / "assets" / "ffmpeg.exe"
        self._reference_key = None
        self._reference_features: List[np.ndarray] = []

    def detect(self, frame: np.ndarray, settings: Settings) -> np.ndarray:
        _checked_settings(settings)
        if frame is None or frame.ndim != 3 or frame.shape[2] != 3:
            raise EngineError("预览帧无效。")
        height, width = frame.shape[:2]
        scale = min(1.0, int(settings.detection_size) / max(height, width))
        resized = cv2.resize(frame, (max(1, round(width * scale)), max(1, round(height * scale)))) if scale < 1 else frame
        self.detector.setInputSize((resized.shape[1], resized.shape[0]))
        self.detector.setScoreThreshold(float(settings.threshold))
        try:
            _, faces = self.detector.detect(resized)
        except cv2.error as exc:
            raise EngineError("人脸检测失败：" + str(exc)) from exc
        if faces is None:
            return np.empty((0, 15), dtype=np.float32)
        faces = faces.astype(np.float32, copy=True)
        faces[:, :14] /= scale
        # Detector output is untrusted numeric input; invalid boxes are discarded.
        valid = np.isfinite(faces).all(axis=1) & (faces[:, 2] > 1) & (faces[:, 3] > 1)
        return faces[valid]

    def _embedding(self, frame, face):
        if self.recognizer is None:
            return None
        try:
            crop = self.recognizer.alignCrop(frame, np.asarray(face, dtype=np.float32))
            feature = self.recognizer.feature(crop).ravel().astype(np.float32)
            norm = float(np.linalg.norm(feature))
            return feature / norm if norm > 1e-8 and np.isfinite(feature).all() else None
        except cv2.error:
            return None

    def _references(self, selection):
        rows = np.asarray(selection.keep_faces, dtype=np.float32)
        if rows.size == 0 or selection.reference_frame is None:
            return []
        rows = rows.reshape(-1, 15)
        key = (id(selection.reference_frame), rows.tobytes())
        if key != self._reference_key:
            self._reference_key = key
            self._reference_features = []
            for row in rows:
                feature = self._embedding(selection.reference_frame, row)
                if feature is not None:
                    self._reference_features.append(feature)
        return self._reference_features

    def keep_indices(self, frame, settings: Settings, selection: Optional[MediaSelection] = None, faces=None) -> Set[int]:
        selection = selection or MediaSelection()
        faces = self.detect(frame, settings) if faces is None else faces
        if len(faces) == 0 or settings.mode == "all":
            return set()
        if settings.mode == "largest":
            return {int(np.argmax(faces[:, 2] * faces[:, 3]))}
        if len(selection.keep_faces) == 0:
            return set()
        reference = selection.reference_frame
        if reference is None or (frame.shape == reference.shape and np.array_equal(frame, reference)):
            # Exact preview frame: spatial selection is reliable without recognition.
            result = set()
            for row in selection.keep_faces:
                overlaps = [_iou(row, face) for face in faces]
                if overlaps and max(overlaps) >= 0.65:
                    result.add(int(np.argmax(overlaps)))
            return result
        refs = self._references(selection)
        if not refs:
            return set()
        features = [self._embedding(frame, face) for face in faces]
        accepted = set()
        pairs = []
        for ri, ref in enumerate(refs):
            scores = [float(np.dot(ref, f)) if f is not None else -1 for f in features]
            order = np.argsort(scores)[::-1]
            best = int(order[0])
            margin = scores[best] - scores[int(order[1])] if len(order) > 1 else 2.0
            # High similarity and separation make exemptions intentionally conservative.
            if scores[best] >= 0.55 and margin >= 0.08:
                pairs.append((scores[best], ri, best))
        used_refs = set()
        for _, ri, fi in sorted(pairs, reverse=True):
            if ri not in used_refs and fi not in accepted:
                used_refs.add(ri)
                accepted.add(fi)
        return accepted

    def keep_flags(self, frame, settings: Settings, selection: Optional[MediaSelection] = None, faces=None) -> List[bool]:
        faces = self.detect(frame, settings) if faces is None else faces
        kept = self.keep_indices(frame, settings, selection, faces)
        return [index in kept for index in range(len(faces))]

    def preview(self, frame, settings: Settings, selection: Optional[MediaSelection] = None) -> np.ndarray:
        selection = selection or MediaSelection()
        faces = self.detect(frame, settings)
        kept = self.keep_indices(frame, settings, selection, faces)
        result = frame.copy()
        for index, face in enumerate(faces):
            if index not in kept:
                _apply_mask(result, face, settings)
        for box in selection.manual_boxes:
            _apply_mask(result, box, settings)
        return result

    @staticmethod
    def _cancelled(cancel_event) -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    @staticmethod
    def _progress(callback, fraction, message):
        if callback is not None:
            callback(max(0.0, min(1.0, float(fraction))), str(message))

    @staticmethod
    def _destination(path: Path, output_dir, suffix: str) -> Path:
        folder = _local_path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / (path.stem + "_已打码" + suffix)
        attempt = 1
        while base.exists():
            base = folder / (path.stem + "_已打码_" + str(attempt) + suffix)
            attempt += 1
        return base

    def process_file(self, path, output_dir, settings: Settings, selection: Optional[MediaSelection] = None,
                     cancel_event=None, on_progress: Optional[Callable] = None) -> Dict:
        _checked_settings(settings)
        path = _local_file(path)
        selection = selection or MediaSelection()
        if self._cancelled(cancel_event):
            raise CancelledError("处理已取消。")
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            return self._process_image(path, output_dir, settings, selection, cancel_event, on_progress)
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            return self._process_video(path, output_dir, settings, selection, cancel_event, on_progress)
        raise EngineError("不支持的文件类型：" + path.suffix)

    def _process_image(self, path, output_dir, settings, selection, cancel_event, callback):
        self._progress(callback, 0.0, "正在检测图片人脸…")
        source_metadata = {}
        frame, alpha = _read_image(path, source_metadata)
        faces = self.detect(frame, settings)
        kept = self.keep_indices(frame, settings, selection, faces)
        result = frame.copy()
        for index, face in enumerate(faces):
            if index not in kept:
                _apply_mask(result, face, settings)
        for box in selection.manual_boxes:
            _apply_mask(result, box, settings)
        destination = self._destination(path, output_dir, ".png")
        temporary = destination.with_name(".part-" + uuid.uuid4().hex + ".png")
        try:
            if self._cancelled(cancel_event):
                raise CancelledError("处理已取消。")
            image = Image.fromarray(result[:, :, ::-1])
            if alpha is not None:
                image.putalpha(Image.fromarray(alpha))
            image.save(str(temporary), format="PNG")
            if self._cancelled(cancel_event):
                raise CancelledError("处理已取消。")
            # os.rename on Windows refuses an existing file, so no output is overwritten.
            os.rename(str(temporary), str(destination))
        finally:
            if temporary.exists():
                temporary.unlink()
        warnings = []
        if source_metadata.get("primary_only"):
            warnings.append("MPO 图片仅处理主图；附加图片及 HDR 辅助信息不会导出，成品为普通 PNG。")
        if not len(faces):
            warnings.append("未检测到人脸；请检查成品，必要时手动添加遮挡框。")
        if settings.mode == "selected" and len(selection.keep_faces) and not kept:
            warnings.append("所选人物未能可靠识别，因此所有检测到的人脸均已遮挡。")
        self._progress(callback, 1.0, "图片处理完成。")
        return {"output": str(destination), "output_path": str(destination), "type": "image",
                "face_count": len(faces), "masked_count": len(faces) - len(kept) + len(selection.manual_boxes),
                "frame_count": 1, "warnings": warnings}

    def _encoder(self, path, temporary, width, height, fps, preserve_audio):
        if not self.ffmpeg_path.is_file():
            raise EngineError("缺少离线视频编码器：" + str(self.ffmpeg_path))
        command = [str(self.ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                   "-protocol_whitelist", "file,pipe", "-f", "rawvideo", "-pixel_format", "bgr24",
                   "-video_size", str(width) + "x" + str(height), "-framerate", format(fps, ".8g"), "-i", "pipe:0"]
        if preserve_audio:
            command += ["-protocol_whitelist", "file,pipe", "-format_whitelist", LOCAL_VIDEO_FORMATS,
                        "-i", str(path), "-map", "0:v:0", "-map", "1:a?"]
        else:
            command += ["-map", "0:v:0", "-an"]
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-map_metadata", "-1", "-movflags", "+faststart"]
        if preserve_audio:
            command += ["-c:a", "aac", "-b:a", "192k", "-af", "apad", "-shortest"]
        command += ["-f", "mp4", str(temporary)]
        try:
            return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, shell=False,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            raise EngineError("无法启动视频编码器：" + str(exc)) from exc

    def _process_video(self, path, output_dir, settings, selection, cancel_event, callback):
        self._progress(callback, 0.0, "正在打开视频…")
        capture = _capture(path)
        encoder = None
        watcher = None
        stderr_reader = None
        stop_watcher = threading.Event()
        errors = deque(maxlen=60)
        destination = self._destination(path, output_dir, ".mp4")
        temporary = destination.with_name(".part-" + uuid.uuid4().hex + ".mp4")
        warnings = ["视频输出按原视频平均帧率转为固定帧率；可变帧率素材的节奏或音画同步可能改变，请复核。"]
        if settings.mode == "largest":
            warnings.append("自动模式每帧保留最大人脸；人物大小变化时可能切换保留对象。可改用手动保留人物。")
        if settings.effect == "blur":
            warnings.append("模糊效果不保证无法辨认；较高隐私要求建议选择马赛克或黑色遮挡。")
        try:
            total = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            if not math.isfinite(fps) or fps < 0.1 or fps > 1000:
                fps = 25.0
                warnings.append("未能获取有效帧率，已采用 25 FPS。")
            if settings.mode == "selected" and len(selection.keep_faces) and selection.reference_frame is None:
                reference, _ = read_preview(path, int(selection.anchor_frame))
                selection = MediaSelection(list(selection.keep_faces), list(selection.manual_boxes), reference, selection.anchor_frame)
            if settings.mode == "selected" and len(selection.keep_faces) and self.recognizer is None:
                warnings.append("人物识别模型不可用；仅参考帧可保留人物，其余帧将保守遮挡所有人脸。")
            ok, frame = capture.read()
            if not ok or frame is None:
                raise EngineError("视频无法解码。")
            width, height = frame.shape[1], frame.shape[0]
            encoder = self._encoder(path, temporary, width, height, fps, settings.preserve_audio)

            def drain_stderr():
                try:
                    for line in iter(encoder.stderr.readline, b""):
                        errors.append(line.decode("utf-8", errors="replace").strip())
                except (OSError, ValueError):
                    pass

            def watch_cancel():
                while not stop_watcher.wait(0.1):
                    if self._cancelled(cancel_event):
                        if encoder.poll() is None:
                            encoder.terminate()
                        return

            stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
            stderr_reader.start()
            watcher = threading.Thread(target=watch_cancel, daemon=True)
            watcher.start()
            tracker = _VideoTracker(fps)
            frame_index = 0
            detections = 0
            masked_count = 0
            zero_frames = 0
            excluded_frames = 0
            while ok and frame is not None:
                if self._cancelled(cancel_event):
                    raise CancelledError("处理已取消。")
                if frame.shape[:2] != (height, width):
                    raise EngineError("视频中途改变了分辨率，无法安全编码。")
                faces = self.detect(frame, settings)
                kept = self.keep_indices(frame, settings, selection, faces)
                detections += len(faces)
                zero_frames += int(not len(faces))
                excluded_frames += int(bool(kept))
                tracks = tracker.update(frame, faces, kept, selection.manual_boxes, frame_index == max(0, int(selection.anchor_frame)))
                result = frame.copy()
                for track in tracks:
                    if not track.kept:
                        _apply_mask(result, track.box, settings, min(0.60, track.missing * 0.035 + track.low_quality * 0.025))
                        masked_count += 1
                try:
                    encoder.stdin.write(np.ascontiguousarray(result).tobytes())
                except (BrokenPipeError, OSError) as exc:
                    if self._cancelled(cancel_event):
                        raise CancelledError("处理已取消。") from exc
                    raise EngineError("视频编码器异常退出：" + " ".join(errors)[-1200:]) from exc
                frame_index += 1
                if frame_index == 1 or frame_index % max(1, round(fps / 3)) == 0:
                    fraction = min(0.98, frame_index / total) if total else 0.0
                    self._progress(callback, fraction, "正在处理视频：第 " + str(frame_index) + " 帧" + (" / " + str(total) if total else ""))
                ok, frame = capture.read()
            if total and frame_index < total - max(2, round(fps * 0.15)):
                raise EngineError("视频提前停止解码：预计 " + str(total) + " 帧，实际仅 " + str(frame_index) + " 帧。未生成成品。")
            if frame_index == 0:
                raise EngineError("视频没有可解码的画面。")
            encoder.stdin.close()
            self._progress(callback, 0.99, "正在保存视频和音频…")
            while encoder.poll() is None:
                if self._cancelled(cancel_event):
                    raise CancelledError("处理已取消。")
                try:
                    encoder.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
            if stderr_reader is not None:
                stderr_reader.join(timeout=2)
            if encoder.returncode != 0:
                if self._cancelled(cancel_event):
                    raise CancelledError("处理已取消。")
                raise EngineError("视频编码失败：" + " ".join(errors)[-1500:])
            if not temporary.is_file() or temporary.stat().st_size < 100:
                raise EngineError("编码器未生成有效的视频文件。")
            if self._cancelled(cancel_event):
                raise CancelledError("处理已取消。")
            os.rename(str(temporary), str(destination))
            if detections == 0:
                warnings.append("整个视频未检测到人脸；请逐段复核，必要时手动添加遮挡。")
            if zero_frames:
                warnings.append(str(zero_frames) + " 帧未检测到人脸（画面也可能没有人脸）；已有轨迹最多补偿约 0.7 秒，漏检需复核。")
            if tracker.uncertain_frames:
                warnings.append(str(tracker.uncertain_frames) + " 帧使用了跟踪补偿或跟踪信心不足，请复核移动、遮挡和转身片段。")
            if tracker.scene_cuts:
                warnings.append("检测到 " + str(tracker.scene_cuts) + " 次明显场景切换；跟踪已重置。")
            if tracker.manual_lost:
                warnings.append(str(tracker.manual_lost) + " 个手动遮挡框因场景切换或持续跟踪失败而停止，相关片段需手动复核。")
            if selection.manual_boxes and not tracker.manual_started:
                warnings.append("手动遮挡参考帧超出视频范围，手动框未生效。")
            if settings.mode == "selected" and len(selection.keep_faces) and not excluded_frames:
                warnings.append("未能可靠识别所选人物；已保守遮挡所有检测到的人脸。")
            self._progress(callback, 1.0, "视频处理完成。")
            return {"output": str(destination), "output_path": str(destination), "type": "video",
                    "face_count": detections, "masked_count": masked_count, "frame_count": frame_index,
                    "fps": fps, "scene_cuts": tracker.scene_cuts, "uncertain_frames": tracker.uncertain_frames,
                    "warnings": warnings}
        finally:
            stop_watcher.set()
            capture.release()
            if encoder is not None:
                if encoder.poll() is None:
                    try:
                        encoder.terminate()
                        encoder.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        encoder.kill()
                        encoder.wait(timeout=3)
                for stream in (encoder.stdin, encoder.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if watcher is not None:
                watcher.join(timeout=1)
            if stderr_reader is not None:
                stderr_reader.join(timeout=1)
            if temporary.exists():
                temporary.unlink()


def process_file(path, output_dir, settings: Settings, selection: Optional[MediaSelection] = None,
                 cancel_event=None, on_progress: Optional[Callable] = None) -> Dict:
    """Convenience API; reuse FaceEngine.process_file for long-running batch jobs."""
    return FaceEngine().process_file(path, output_dir, settings, selection, cancel_event, on_progress)
