"""Local camera discovery and capture.

Wraps OpenCV's VideoCapture for V4L2/RTSP and (optionally) Aravis for GenICam
industrial cameras. Exposes a small sync interface; the asyncio layer pulls
frames off a background thread and pushes them through the frame pipeline.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional

import cv2
import numpy as np

from ..models.schemas import CameraInfo, CameraKind

log = logging.getLogger(__name__)


def _probe_v4l2(idx: int) -> Optional[CameraInfo]:
    """Try to open /dev/video<idx> briefly. Returns CameraInfo if it works."""
    dev = f"/dev/video{idx}"
    if not Path(dev).exists():
        return None
    cap = cv2.VideoCapture(idx)
    if not cap.isOpened():
        return None
    try:
        ok, _ = cap.read()
        if not ok:
            return None
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return CameraInfo(
            id=dev,
            kind=CameraKind.V4L2,
            label=f"V4L2 #{idx} ({w}x{h})",
            resolution=[w, h] if w and h else None,
        )
    finally:
        cap.release()


def list_v4l2_cameras(max_index: int = 8) -> List[CameraInfo]:
    """Enumerate /dev/video0..N, returning the ones that actually open."""
    found: List[CameraInfo] = []
    for i in range(max_index):
        info = _probe_v4l2(i)
        if info is not None:
            found.append(info)
    return found


def list_rtsp_cameras(config_path: Optional[Path] = None) -> List[CameraInfo]:
    """Read optional `rtsp_cameras.yaml` so users can pre-register IP cameras."""
    if config_path is None:
        config_path = Path(__file__).resolve().parents[2] / "config" / "rtsp_cameras.yaml"
    if not config_path.exists():
        return []
    try:
        import yaml  # local import; PyYAML is in deps
    except ImportError:
        log.warning("PyYAML missing — cannot read %s", config_path)
        return []
    with config_path.open() as fh:
        data = yaml.safe_load(fh) or {}
    out: List[CameraInfo] = []
    for entry in data.get("cameras", []):
        out.append(
            CameraInfo(
                id=entry["url"],
                kind=CameraKind.RTSP,
                label=entry.get("label", entry["url"]),
            )
        )
    return out


def list_aravis_cameras() -> List[CameraInfo]:
    """Optional: GenICam cameras via `aravis` Python bindings."""
    try:
        import aravis  # type: ignore
    except ImportError:
        return []
    out: List[CameraInfo] = []
    try:
        aravis.update_device_list()
        for i in range(aravis.get_n_devices()):
            cam_id = aravis.get_device_id(i)
            out.append(
                CameraInfo(
                    id=f"aravis://{cam_id}",
                    kind=CameraKind.ARAVIS,
                    label=f"Aravis: {cam_id}",
                )
            )
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("Aravis discovery failed: %s", exc)
    return out


def list_cameras() -> List[CameraInfo]:
    cams: List[CameraInfo] = []
    cams.extend(list_v4l2_cameras())
    cams.extend(list_rtsp_cameras())
    cams.extend(list_aravis_cameras())
    return cams


class CameraCapture:
    """Synchronous capture thread; the asyncio layer pulls .read().

    Keeps a single VideoCapture and a thread that continuously reads the
    latest frame. read() always returns the freshest frame (drop older ones).
    """

    def __init__(self, camera_id: str, target_fps: int = 15) -> None:
        self.camera_id = camera_id
        self.target_fps = target_fps
        self._cap: Optional[cv2.VideoCapture] = None
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def open(self) -> None:
        if self.camera_id.startswith("/dev/video"):
            idx = int(self.camera_id.replace("/dev/video", ""))
            self._cap = cv2.VideoCapture(idx)
        elif self.camera_id.startswith("rtsp://") or self.camera_id.startswith("http://"):
            self._cap = cv2.VideoCapture(self.camera_id)
        else:
            # Best-effort: treat as an OpenCV index/path.
            try:
                idx = int(self.camera_id)
                self._cap = cv2.VideoCapture(idx)
            except ValueError:
                self._cap = cv2.VideoCapture(self.camera_id)
        if not self._cap or not self._cap.isOpened():
            raise RuntimeError(f"Failed to open camera: {self.camera_id}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._cap is not None
        delay = 1.0 / max(self.target_fps, 1)
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                continue
            with self._lock:
                self._frame = frame
            self._stop.wait(delay)

    def read(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._cap:
            self._cap.release()
        self._cap = None
        self._frame = None

    # Context-manager sugar
    def __enter__(self) -> "CameraCapture":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()