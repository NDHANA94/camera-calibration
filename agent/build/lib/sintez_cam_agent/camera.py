"""Local camera capture for the remote agent. Mirrors the server's
CameraCapture but lives in the agent package so the server doesn't have to
be installed on the remote box."""
from __future__ import annotations

import logging
import threading
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)


class CameraCapture:
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
        elif self.camera_id.startswith(("rtsp://", "http://")):
            self._cap = cv2.VideoCapture(self.camera_id)
        else:
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


# How long to wait for one device to yield a frame before giving up on it.
# v4l2loopback and codec/M2M nodes can block forever on read() when nothing
# is feeding them, so each probe runs in its own subprocess that we hard-kill
# on timeout -- a read() blocked in the kernel can't be interrupted from a
# Python thread, only by killing the process holding it.
_PROBE_TIMEOUT_S = 4.0

# Child program: open one device, grab a frame, print "W H", exit 0.
# Devices that don't open / don't yield a frame exit non-zero; devices that
# hang are killed by the parent's subprocess timeout.
_PROBE_SRC = (
    "import sys, cv2\n"
    "cap = cv2.VideoCapture(sys.argv[1])\n"
    "try:\n"
    "    if not cap.isOpened(): sys.exit(2)\n"
    "    ok, _ = cap.read()\n"
    "    if not ok: sys.exit(3)\n"
    "    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))\n"
    "    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))\n"
    "    sys.stdout.write('%d %d' % (w, h))\n"
    "finally:\n"
    "    cap.release()\n"
)


def _probe_device(dev: str, timeout: float = _PROBE_TIMEOUT_S) -> Optional[dict]:
    """Probe one /dev/videoN in a subprocess so a hung read() can be killed.

    Returns a camera dict ``{"id", "label"}`` if the device yields a frame,
    else ``None`` (not a capture device, or not producing frames right now).
    """
    import subprocess, sys
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SRC, dev],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("camera probe for %s timed out (%.0fs); skipping -- "
                    "likely an idle loopback/codec node", dev, timeout)
        return None
    except Exception as exc:
        log.debug("camera probe for %s failed to launch: %s", dev, exc)
        return None
    if proc.returncode != 0:
        return None
    try:
        w, h = (int(x) for x in proc.stdout.split())
    except Exception:
        return None
    return {"id": dev, "label": f"{dev}  ({w}×{h})"}


def list_local_cameras() -> list[dict]:
    """Enumerate usable /dev/video* capture devices.

    Each device is probed in its own subprocess with a hard timeout, so a
    single unresponsive node (an idle v4l2loopback or a Jetson codec/M2M
    device -- both block forever on read()) can't hang enumeration.  Probes
    run in parallel, so total time is bounded by the slowest single device.
    Falls back to ``v4l2-ctl --list-devices`` only if no device yields a
    frame.
    """
    import glob, os, stat, shutil, subprocess
    from concurrent.futures import ThreadPoolExecutor

    devs: list[str] = []
    for dev in sorted(glob.glob("/dev/video*")):
        # Skip non-character-device matches such as the /dev/video directory.
        try:
            if stat.S_ISCHR(os.stat(dev).st_mode):
                devs.append(dev)
        except OSError:
            continue

    out: list[dict] = []
    if devs:
        # Each _probe_device blocks on its own subprocess, so these worker
        # threads only wait on I/O; all probes run concurrently.
        with ThreadPoolExecutor(max_workers=len(devs)) as ex:
            for info in ex.map(_probe_device, devs):
                if info:
                    out.append(info)
    out.sort(key=lambda c: c["id"])

    if not out and shutil.which("v4l2-ctl"):
        try:
            r = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("/dev/video"):
                    out.append({"id": line, "label": line})
        except Exception:
            pass
    return out