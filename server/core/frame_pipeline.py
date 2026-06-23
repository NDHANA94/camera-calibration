"""Frame pipeline: chessboard detection, sub-pixel refinement, overlay,
JPEG encoding. Pure functions so they can be tested without a live camera.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from . import quality


@dataclass
class FrameResult:
    jpeg: bytes                              # encoded JPEG for streaming
    width: int
    height: int
    board_found: bool
    corners: Optional[np.ndarray] = None    # shape (N, 1, 2)
    blur_score: float = 0.0
    capture_taken: bool = False             # true if this frame was saved as a capture
    capture_highlight: bool = False         # true for visual flash after capture


def detect_board(gray: np.ndarray, board_w: int, board_h: int) -> Optional[np.ndarray]:
    """Find inner corners of a chessboard; refine to sub-pixel. None if not found."""
    pattern = (board_w, board_h)
    found, corners = cv2.findChessboardCorners(gray, pattern, None)
    if not found:
        return None
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners = cv2.cornerSubPix(
        gray, corners, winSize=(11, 11), zeroZone=(-1, -1), criteria=criteria
    )
    return corners


def draw_overlay(
    frame_bgr: np.ndarray,
    corners: Optional[np.ndarray],
    board_found: bool,
    board_w: int,
    board_h: int,
    capture_highlight: bool,
    blur_score: float,
    blur_threshold: float,
    captures: int,
    required: int,
    hint: str,
) -> np.ndarray:
    """Draw corners, capture badge, and a hint on a copy of the frame."""
    img = frame_bgr.copy()
    if board_found and corners is not None:
        # board_w × board_h is the correct pattern size for drawChessboardCorners
        cv2.drawChessboardCorners(img, (board_w, board_h), corners, True)

    color = (0, 255, 0) if board_found else (0, 0, 255)
    cv2.putText(
        img,
        f"Board: {'OK' if board_found else 'NO'}  captures: {captures}/{required}",
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        color,
        2,
    )
    cv2.putText(
        img,
        f"Blur: {blur_score:.1f} (thr {blur_threshold:.0f})",
        (10, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 255),
        1,
    )
    cv2.putText(
        img,
        hint,
        (10, img.shape[0] - 12),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 0),
        1,
    )
    if capture_highlight:
        cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 255, 255), 4)
    return img


class Pipeline:
    """Stateful processor: tracks board stability across frames and decides
    when to auto-capture. One Pipeline instance per session.

    Capture quality strategy:
    - Board must be stable (low corner drift) for `stable_frames` consecutive frames.
    - The sharpest (highest Laplacian variance) corners in that window are saved.
    - After a capture the board centroid must move >= `min_movement` pixels before
      another auto-capture is accepted, enforcing spatial diversity.
    - `force_capture` bypasses stability and movement checks (manual override).
    """

    def __init__(
        self,
        board_w: int,
        board_h: int,
        required_captures: int = 10,
        blur_threshold: float = quality.DEFAULT_BLUR_THRESHOLD,
        stable_frames: int = quality.DEFAULT_STABLE_FRAMES,
        min_movement: float = 60.0,
    ) -> None:
        self.board_w = board_w
        self.board_h = board_h
        self.required_captures = required_captures
        self.blur_threshold = blur_threshold
        self.stable_frames = stable_frames
        self.min_movement = min_movement
        self.captures: list[np.ndarray] = []
        self._stable_count = 0
        self._last_corners: Optional[np.ndarray] = None
        self._capture_highlight_frames = 0
        self._last_captured_centroid: Optional[np.ndarray] = None
        # Best frame tracked within the current stable window
        self._best_blur_in_window: float = 0.0
        self._best_corners_in_window: Optional[np.ndarray] = None

    def _centroid(self, corners: np.ndarray) -> np.ndarray:
        return corners.reshape(-1, 2).mean(axis=0)

    def _has_enough_movement(self, corners: np.ndarray) -> bool:
        if self._last_captured_centroid is None:
            return True
        dist = float(np.linalg.norm(self._centroid(corners) - self._last_captured_centroid))
        return dist >= self.min_movement

    def process(
        self,
        frame_bgr: np.ndarray,
        hint_fn,
        force_capture: bool = False,
    ) -> FrameResult:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        blur = quality.laplacian_variance(gray)
        sharp = blur >= self.blur_threshold
        corners = detect_board(gray, self.board_w, self.board_h)
        board_found = corners is not None

        # Stability: board present, sharp, corners not drifting between frames
        stable = board_found and sharp
        if stable and self._last_corners is not None:
            try:
                if np.linalg.norm(corners - self._last_corners) > 12.0:
                    stable = False
            except Exception:
                stable = False

        if stable:
            self._stable_count += 1
            if blur > self._best_blur_in_window:
                self._best_blur_in_window = blur
                self._best_corners_in_window = corners
        else:
            self._stable_count = 0
            self._best_blur_in_window = 0.0
            self._best_corners_in_window = None

        self._last_corners = corners if board_found else self._last_corners

        capture_taken = False
        auto_ok = (
            stable
            and self._stable_count >= self.stable_frames
            and len(self.captures) < self.required_captures
            and self._has_enough_movement(corners)
        )
        force_ok = (
            force_capture
            and board_found
            and len(self.captures) < self.required_captures
        )

        if auto_ok or force_ok:
            # Prefer the sharpest corners from the stable window; fall back to current
            best = self._best_corners_in_window if (self._best_corners_in_window is not None and not force_ok) else corners
            self.captures.append(best)
            self._last_captured_centroid = self._centroid(best)
            self._capture_highlight_frames = 6
            self._stable_count = 0
            self._best_blur_in_window = 0.0
            self._best_corners_in_window = None
            capture_taken = True

        if self._capture_highlight_frames > 0:
            self._capture_highlight_frames -= 1
        capture_highlight = self._capture_highlight_frames > 0 or capture_taken

        hint = hint_fn(self.captures)
        overlay = draw_overlay(
            frame_bgr,
            corners,
            board_found,
            self.board_w,
            self.board_h,
            capture_highlight,
            blur,
            self.blur_threshold,
            len(self.captures),
            self.required_captures,
            hint,
        )
        ok, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return FrameResult(
            jpeg=buf.tobytes(),
            width=frame_bgr.shape[1],
            height=frame_bgr.shape[0],
            board_found=board_found,
            corners=corners,
            blur_score=blur,
            capture_taken=capture_taken,
            capture_highlight=capture_highlight,
        )
