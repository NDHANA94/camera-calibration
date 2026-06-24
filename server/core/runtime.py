"""In-memory per-session runtime state used by the streaming layer.

Kept separate from sessions.py (which is the persistent on-disk index) so
that long-lived frame pipelines don't pollute the route module.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Dict, Optional

import numpy as np

from .frame_pipeline import DualCameraPipeline, Pipeline, StereoPipeline
from ..models.schemas import CameraMode, Chessboard


def _select_pipeline(chessboard: Chessboard):
    """Pick the right pipeline class for a chessboard's mode."""
    if chessboard.mode == CameraMode.MONO:
        return Pipeline
    if chessboard.mode == CameraMode.STEREO_LR:
        return StereoPipeline
    if chessboard.mode == CameraMode.STEREO_SEPARATE:
        return DualCameraPipeline
    # Fallback to mono
    return Pipeline


class SessionRuntime:
    def __init__(self, session_id: str, chessboard: Chessboard) -> None:
        self.session_id = session_id
        self.chessboard = chessboard
        # Backward-compat: ``stereo`` reflects any stereo mode.
        self.stereo = chessboard.is_stereo
        self.mode = chessboard.mode
        pipeline_cls = _select_pipeline(chessboard)
        self.pipeline = pipeline_cls(
            board_w=chessboard.inner_corners_x,
            board_h=chessboard.inner_corners_y,
            required_captures=getattr(chessboard, "required_captures", 15),
        )
        self.profile = chessboard  # legacy attribute name preserved
        self.frame_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=2)
        self.event_queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=64)
        self.image_size: Optional[tuple[int, int]] = None
        self._lock = asyncio.Lock()
        self.captures_count = 0
        self.aborted = False

    async def record_capture(self, frame_bgr, corners) -> None:
        """Persist a captured frame's corners to disk for later calibration."""
        from ..core.storage import session_dir
        from .sessions_helpers import bump_capture  # see imports below

        sdir = session_dir(self.session_id)
        corners_dir = sdir / "corners"
        corners_dir.mkdir(parents=True, exist_ok=True)
        idx = len(list(corners_dir.glob("*.npy")))
        np.save(corners_dir / f"{idx:04d}.npy", corners)
        # Also keep a small PNG snapshot for reproducibility.
        try:
            import cv2
            cv2.imwrite(str(sdir / "frames" / f"{idx:04d}.png"), frame_bgr)
        except Exception:
            pass
        # First frame establishes image_size for calibration.
        if self.image_size is None:
            self.image_size = (frame_bgr.shape[1], frame_bgr.shape[0])
            (sdir / "image_size.json").write_text(
                json.dumps({"image_size": list(self.image_size)})
            )
        self.captures_count += 1
        bump_capture(self.session_id)


_runtimes: Dict[str, SessionRuntime] = {}
_runtimes_lock = asyncio.Lock()


async def get_runtime(session_id: str) -> SessionRuntime:
    async with _runtimes_lock:
        if session_id not in _runtimes:
            from .sessions_helpers import load_chessboard_for_session
            cb = load_chessboard_for_session(session_id)
            _runtimes[session_id] = SessionRuntime(session_id, cb)
        return _runtimes[session_id]


async def drop_runtime(session_id: str) -> None:
    async with _runtimes_lock:
        _runtimes.pop(session_id, None)
