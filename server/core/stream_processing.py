"""Transport-agnostic frame processing shared by the local WebSocket loop and
the remote (SSH agent) SSE proxy.

Both transports decode a BGR frame (or, for STEREO_SEPARATE, a pair of
BGR frames) and run it through the per-session pipeline (mono, stereo L|R, or
stereo-separate).  The pipeline persists captures to disk and returns the
annotated JPEG plus a few event dicts.  Keeping this here lets the local and
remote paths share identical detection/capture behaviour -- so the calibration
result is the same regardless of where the camera lives.
"""
from __future__ import annotations

import json
from typing import List, Tuple, Union

import cv2
import numpy as np

from ..models.schemas import CameraMode
from .guidance import coverage_tip
from .sessions_helpers import bump_capture
from .storage import session_dir

# A frame (or pair of frames) handed to the pipeline.  For mono / stereo L|R
# it's a single np.ndarray.  For stereo_separate it's a (left, right) tuple.
FrameInput = Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]


def _save_capture(runtime, frame_input: FrameInput, result) -> None:
    """Persist one capture (corners + PNG) and establish image_size.

    Mono: corners/NNNN.npy.  Stereo (any mode): corners_left/NNNN.npy +
    corners_right/NNNN.npy (the per-eye image_size is the eye's width)."""
    sdir = session_dir(runtime.session_id)
    stereo = bool(getattr(runtime, "stereo", False))

    if runtime.image_size is None:
        if stereo:
            # Per-eye image_size = the width of the LEFT image
            if isinstance(frame_input, tuple):
                l = frame_input[0]
            else:
                # stereo L|R: per-eye width = full width / 2
                l = frame_input[:, : frame_input.shape[1] // 2]
            runtime.image_size = (l.shape[1], l.shape[0])
        else:
            runtime.image_size = (result.width, result.height)
        (sdir / "image_size.json").write_text(
            json.dumps({"image_size": list(runtime.image_size), "stereo": stereo})
        )

    if stereo:
        ld = sdir / "corners_left"
        rd = sdir / "corners_right"
        ld.mkdir(parents=True, exist_ok=True)
        rd.mkdir(parents=True, exist_ok=True)
        idx = len(list(ld.glob("*.npy")))
        np.save(ld / f"{idx:04d}.npy", result.corners)
        np.save(rd / f"{idx:04d}.npy", result.corners_right)
    else:
        cd = sdir / "corners"
        cd.mkdir(parents=True, exist_ok=True)
        idx = len(list(cd.glob("*.npy")))
        np.save(cd / f"{idx:04d}.npy", result.corners)

    # Save the captured frame WITH the detected-corner annotations drawn on it.
    # For stereo_separate, save the side-by-side combined frame; otherwise save
    # the raw input frame.
    try:
        (sdir / "frames").mkdir(parents=True, exist_ok=True)
        if stereo:
            annotated = _annotate_stereo_capture(frame_input, result, runtime)
        else:
            annotated = _annotate_mono_capture(frame_input, result, runtime)
        cv2.imwrite(str(sdir / "frames" / f"{idx:04d}.png"), annotated)
    except Exception:
        pass

    runtime.captures_count += 1
    bump_capture(runtime.session_id)


def _annotate_mono_capture(frame_bgr: np.ndarray, result, runtime) -> np.ndarray:
    bw = runtime.chessboard.inner_corners_x
    bh = runtime.chessboard.inner_corners_y
    img = frame_bgr.copy()
    if result.corners is not None:
        cv2.drawChessboardCorners(img, (bw, bh), result.corners, True)
    return img


def _annotate_stereo_capture(
    frame_input: FrameInput,
    result,
    runtime,
) -> np.ndarray:
    """Build a side-by-side annotated frame for any stereo mode."""
    bw = runtime.chessboard.inner_corners_x
    bh = runtime.chessboard.inner_corners_y
    if isinstance(frame_input, tuple):
        left, right = frame_input
        combined = np.hstack([left, right])
        half = left.shape[1]
    else:
        combined = frame_input.copy()
        half = combined.shape[1] // 2
    if result.corners is not None:
        cv2.drawChessboardCorners(combined[:, :half], (bw, bh), result.corners, True)
    if result.corners_right is not None:
        cv2.drawChessboardCorners(combined[:, half:half * 2], (bw, bh), result.corners_right, True)
    return combined


def process_frame(runtime, frame_input: FrameInput, *, force_capture: bool = False
                  ) -> Tuple[bytes, List[dict]]:
    """Process one frame (or pair of frames) through the pipeline.

    Mono / stereo L|R: pass a single BGR frame.
    Stereo-separate: pass a (left, right) tuple.

    Returns (jpeg_bytes, events).  Transport-neutral -- caller decides how to
    forward them."""
    force = force_capture or getattr(runtime, "force_capture", False)
    if getattr(runtime, "force_capture", False):
        runtime.force_capture = False

    result = runtime.pipeline.process(
        frame_input, hint_fn=lambda caps: coverage_tip(caps), force_capture=force,
    )

    events: List[dict] = []
    if result.capture_taken:
        _save_capture(runtime, frame_input, result)
        events.append({
            "type": "capture",
            "n": runtime.captures_count,
            "blur": round(result.blur_score, 1),
        })

    runtime._frame_count = getattr(runtime, "_frame_count", 0) + 1
    fc = runtime._frame_count
    if fc % 10 == 0:
        events.append({
            "type": "status",
            "board": result.board_found,
            "blur": round(result.blur_score, 1),
        })
    if fc % 60 == 0 or result.capture_taken:
        events.append({
            "type": "hint",
            "message": coverage_tip(runtime.pipeline.captures),
        })

    return result.jpeg, events
