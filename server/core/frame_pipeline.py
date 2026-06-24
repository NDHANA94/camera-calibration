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
    corners: Optional[np.ndarray] = None    # shape (N, 1, 2); LEFT corners in stereo
    blur_score: float = 0.0
    capture_taken: bool = False             # true if this frame was saved as a capture
    capture_highlight: bool = False         # true for visual flash after capture
    corners_right: Optional[np.ndarray] = None  # RIGHT corners (stereo only)


# findChessboardCorners is O(pixels) and pathologically slow on multi-megapixel
# frames -- ~12 s for a 1920x1200 half and ~52 s for a 3840x1200 frame when no
# board is present.  We therefore search on a downscaled copy and refine the
# found corners back at full resolution.  960 px on the long edge keeps a
# typical board comfortably detectable while making each search fast.  Detection
# cost scales with pixel count, so this is the main fps lever for large frames.
_DETECT_MAX_DIM = 720


def detect_board(gray: np.ndarray, board_w: int, board_h: int) -> Optional[np.ndarray]:
    """Find inner corners of a chessboard; refine to sub-pixel. None if not found.

    Searches a downscaled copy for speed, then refines corners on the full-res
    image so accuracy is unchanged."""
    pattern = (board_w, board_h)
    h, w = gray.shape[:2]
    scale = 1.0
    small = gray
    if max(h, w) > _DETECT_MAX_DIM:
        scale = _DETECT_MAX_DIM / float(max(h, w))
        small = cv2.resize(
            gray, (int(round(w * scale)), int(round(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    flags = (cv2.CALIB_CB_ADAPTIVE_THRESH
             | cv2.CALIB_CB_NORMALIZE_IMAGE
             | cv2.CALIB_CB_FAST_CHECK)
    found, corners = cv2.findChessboardCorners(small, pattern, flags)
    if not found:
        return None
    if scale != 1.0:
        corners = corners.astype(np.float32) / scale
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners = cv2.cornerSubPix(
        gray, corners.astype(np.float32), winSize=(11, 11),
        zeroZone=(-1, -1), criteria=criteria,
    )
    return corners


# Preview frames are downscaled before JPEG encoding: detection/refinement
# already ran at full resolution, so the browser preview doesn't need 4K -- a
# smaller preview makes encoding + transfer much cheaper (higher effective fps).
_PREVIEW_MAX_W = 1600


def encode_preview(img_bgr: np.ndarray, quality: int = 80) -> bytes:
    """JPEG-encode a frame for streaming, downscaling wide frames first."""
    w = img_bgr.shape[1]
    if w > _PREVIEW_MAX_W:
        s = _PREVIEW_MAX_W / float(w)
        img_bgr = cv2.resize(
            img_bgr, (_PREVIEW_MAX_W, int(round(img_bgr.shape[0] * s))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", img_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buf.tobytes()


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
        return FrameResult(
            jpeg=encode_preview(overlay),
            width=frame_bgr.shape[1],
            height=frame_bgr.shape[0],
            board_found=board_found,
            corners=corners,
            blur_score=blur,
            capture_taken=capture_taken,
            capture_highlight=capture_highlight,
        )


class DualCameraPipeline:
    """Stereo pipeline for two INDEPENDENT camera devices.

    Unlike :class:`StereoPipeline` (which splits a single side-by-side frame),
    this pipeline consumes two separate frames -- one from the LEFT camera and
    one from the RIGHT camera -- and pairs them at capture time.

    Stability rules mirror :class:`Pipeline`: board must be detected AND sharp
    on BOTH sides for `stable_frames` consecutive rounds before auto-capture,
    and a centroid-movement gate enforces pose diversity.

    A single side-by-side preview image (LEFT on the left, RIGHT on the right)
    is emitted for the live view, so the UI does not need to know whether
    stereo came from one camera or two.
    """

    def __init__(
        self,
        board_w: int,
        board_h: int,
        required_captures: int = 15,
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
        self.captures: list[np.ndarray] = []           # left corners
        self.captures_right: list[np.ndarray] = []      # right corners
        self._stable_count = 0
        self._last_left: Optional[np.ndarray] = None
        self._capture_highlight_frames = 0
        self._last_captured_centroid: Optional[np.ndarray] = None
        self._best_blur_in_window: float = 0.0
        self._best_left_in_window: Optional[np.ndarray] = None
        self._best_right_in_window: Optional[np.ndarray] = None

    def _centroid(self, corners: np.ndarray) -> np.ndarray:
        return corners.reshape(-1, 2).mean(axis=0)

    def _has_enough_movement(self, corners: np.ndarray) -> bool:
        if self._last_captured_centroid is None:
            return True
        dist = float(np.linalg.norm(self._centroid(corners) - self._last_captured_centroid))
        return dist >= self.min_movement

    def process(
        self,
        frames: tuple[np.ndarray, np.ndarray],
        hint_fn,
        force_capture: bool = False,
    ) -> FrameResult:
        """Accept ``(left_frame, right_frame)``.  Detects + captures both.

        If only one frame is given (or sizes differ wildly) we degrade
        gracefully: still process whatever we have, but skip capture.
        """
        left, right = frames
        if left is None or right is None:
            # Nothing to do; emit a tiny status-only frame so the loop keeps
            # producing events without crashing.
            blank = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(blank, "waiting for left + right frames", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            return FrameResult(
                jpeg=encode_preview(blank), width=blank.shape[1], height=blank.shape[0],
                board_found=False, corners=None, blur_score=0.0,
                capture_taken=False, capture_highlight=False,
            )

        grayL = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        grayR = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
        blurL = quality.laplacian_variance(grayL)
        blurR = quality.laplacian_variance(grayR)
        blur = min(blurL, blurR)
        sharp = blur >= self.blur_threshold

        cornersL = detect_board(grayL, self.board_w, self.board_h)
        cornersR = detect_board(grayR, self.board_w, self.board_h)
        board_found = cornersL is not None and cornersR is not None

        stable = board_found and sharp
        if stable and self._last_left is not None:
            try:
                if np.linalg.norm(cornersL - self._last_left) > 12.0:
                    stable = False
            except Exception:
                stable = False

        if stable:
            self._stable_count += 1
            if blur > self._best_blur_in_window:
                self._best_blur_in_window = blur
                self._best_left_in_window = cornersL
                self._best_right_in_window = cornersR
        else:
            self._stable_count = 0
            self._best_blur_in_window = 0.0
            self._best_left_in_window = None
            self._best_right_in_window = None

        self._last_left = cornersL if board_found else self._last_left

        capture_taken = False
        auto_ok = (
            stable
            and self._stable_count >= self.stable_frames
            and len(self.captures) < self.required_captures
            and self._has_enough_movement(cornersL)
        )
        force_ok = (
            force_capture
            and board_found
            and len(self.captures) < self.required_captures
        )

        if auto_ok or force_ok:
            use_l = self._best_left_in_window if (self._best_left_in_window is not None and not force_ok) else cornersL
            use_r = self._best_right_in_window if (self._best_right_in_window is not None and not force_ok) else cornersR
            self.captures.append(use_l)
            self.captures_right.append(use_r)
            self._last_captured_centroid = self._centroid(use_l)
            self._capture_highlight_frames = 6
            self._stable_count = 0
            self._best_blur_in_window = 0.0
            self._best_left_in_window = None
            self._best_right_in_window = None
            capture_taken = True

        if self._capture_highlight_frames > 0:
            self._capture_highlight_frames -= 1
        capture_highlight = self._capture_highlight_frames > 0 or capture_taken

        hint = hint_fn(self.captures)

        # Compose a side-by-side preview so the live view shows both feeds.
        h = max(left.shape[0], right.shape[0])
        # Match heights (resize the shorter one) so the preview is rectangular.
        def _fit(img: np.ndarray) -> np.ndarray:
            if img.shape[0] == h:
                return img
            scale = h / float(img.shape[0])
            return cv2.resize(img, (int(round(img.shape[1] * scale)), h),
                              interpolation=cv2.INTER_AREA)
        L = _fit(left); R = _fit(right)
        combined = np.hstack([L, R])
        # Annotate L/R labels + per-side status.
        half = L.shape[1]
        cv2.putText(combined, f"L:{'OK' if cornersL is not None else 'NO'}  captures: {len(self.captures)}/{self.required_captures}",
                    (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0) if cornersL is not None else (0, 0, 255), 2)
        cv2.putText(combined, f"R:{'OK' if cornersR is not None else 'NO'}",
                    (half + 8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0) if cornersR is not None else (0, 0, 255), 2)
        cv2.putText(combined, f"blur: {blur:.0f} (thr {self.blur_threshold:.0f})",
                    (8, combined.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1)
        cv2.putText(combined, hint, (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 0), 1)
        if capture_highlight:
            cv2.rectangle(combined, (0, 0),
                          (combined.shape[1] - 1, combined.shape[0] - 1),
                          (0, 255, 255), 4)

        return FrameResult(
            jpeg=encode_preview(combined),
            width=combined.shape[1],
            height=combined.shape[0],
            board_found=board_found,
            corners=cornersL,
            corners_right=cornersR,
            blur_score=blur,
            capture_taken=capture_taken,
            capture_highlight=capture_highlight,
        )


def split_lr(frame_bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split a side-by-side stereo frame into (left, right) halves.

    Corner coordinates detected in each half are relative to that half, which
    is exactly what stereo calibration expects (per-eye image points)."""
    w = frame_bgr.shape[1]
    half = w // 2
    return frame_bgr[:, :half], frame_bgr[:, half:half * 2]


class StereoPipeline:
    """Side-by-side stereo variant of :class:`Pipeline`.

    Each frame is split into left/right halves.  A capture is taken only when
    the board is detected (and stable + sharp) in BOTH halves; the left and
    right corner sets are saved as a synchronized pair for stereoCalibrate.
    The streamed overlay shows both halves with corners and L/R labels.
    """

    def __init__(
        self,
        board_w: int,
        board_h: int,
        required_captures: int = 10,
        blur_threshold: float = quality.DEFAULT_BLUR_THRESHOLD,
        stable_frames: int = quality.DEFAULT_STABLE_FRAMES,
        min_movement: float = 40.0,
    ) -> None:
        self.board_w = board_w
        self.board_h = board_h
        self.required_captures = required_captures
        self.blur_threshold = blur_threshold
        self.stable_frames = stable_frames
        self.min_movement = min_movement
        # Left corner sets drive coverage/hints; right is the paired partner.
        self.captures: list[np.ndarray] = []
        self.captures_right: list[np.ndarray] = []
        self._stable_count = 0
        self._last_left: Optional[np.ndarray] = None
        self._capture_highlight_frames = 0
        self._last_captured_centroid: Optional[np.ndarray] = None
        self._best_blur = 0.0
        self._best_left: Optional[np.ndarray] = None
        self._best_right: Optional[np.ndarray] = None

    def _centroid(self, corners: np.ndarray) -> np.ndarray:
        return corners.reshape(-1, 2).mean(axis=0)

    def _has_enough_movement(self, corners: np.ndarray) -> bool:
        if self._last_captured_centroid is None:
            return True
        dist = float(np.linalg.norm(self._centroid(corners) - self._last_captured_centroid))
        return dist >= self.min_movement

    def process(self, frame_bgr: np.ndarray, hint_fn, force_capture: bool = False) -> FrameResult:
        left, right = split_lr(frame_bgr)
        grayL = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        grayR = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)
        blurL = quality.laplacian_variance(grayL)
        blurR = quality.laplacian_variance(grayR)
        blur = min(blurL, blurR)
        sharp = blur >= self.blur_threshold

        cornersL = detect_board(grayL, self.board_w, self.board_h)
        cornersR = detect_board(grayR, self.board_w, self.board_h)
        board_found = cornersL is not None and cornersR is not None

        stable = board_found and sharp
        if stable and self._last_left is not None:
            try:
                if np.linalg.norm(cornersL - self._last_left) > 12.0:
                    stable = False
            except Exception:
                stable = False

        if stable:
            self._stable_count += 1
            if blur > self._best_blur:
                self._best_blur = blur
                self._best_left = cornersL
                self._best_right = cornersR
        else:
            self._stable_count = 0
            self._best_blur = 0.0
            self._best_left = None
            self._best_right = None

        self._last_left = cornersL if board_found else self._last_left

        capture_taken = False
        auto_ok = (
            stable
            and self._stable_count >= self.stable_frames
            and len(self.captures) < self.required_captures
            and self._has_enough_movement(cornersL)
        )
        force_ok = (
            force_capture
            and board_found
            and len(self.captures) < self.required_captures
        )

        if auto_ok or force_ok:
            use_l = self._best_left if (self._best_left is not None and not force_ok) else cornersL
            use_r = self._best_right if (self._best_right is not None and not force_ok) else cornersR
            self.captures.append(use_l)
            self.captures_right.append(use_r)
            self._last_captured_centroid = self._centroid(use_l)
            self._capture_highlight_frames = 6
            self._stable_count = 0
            self._best_blur = 0.0
            self._best_left = None
            self._best_right = None
            capture_taken = True

        if self._capture_highlight_frames > 0:
            self._capture_highlight_frames -= 1
        capture_highlight = self._capture_highlight_frames > 0 or capture_taken

        hint = hint_fn(self.captures)
        # Draw overlays onto views of the original frame (shared memory) so the
        # combined side-by-side image is annotated in place.
        img = frame_bgr.copy()
        half = frame_bgr.shape[1] // 2
        imgL = img[:, :half]
        imgR = img[:, half:half * 2]
        if cornersL is not None:
            cv2.drawChessboardCorners(imgL, (self.board_w, self.board_h), cornersL, True)
        if cornersR is not None:
            cv2.drawChessboardCorners(imgR, (self.board_w, self.board_h), cornersR, True)
        color = (0, 255, 0) if board_found else (0, 0, 255)
        cv2.putText(img, f"L:{'OK' if cornersL is not None else 'NO'}", (8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0) if cornersL is not None else (0, 0, 255), 2)
        cv2.putText(img, f"R:{'OK' if cornersR is not None else 'NO'}", (half + 8, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0) if cornersR is not None else (0, 0, 255), 2)
        cv2.putText(img, f"stereo  captures: {len(self.captures)}/{self.required_captures}",
                    (8, img.shape[0] - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        cv2.putText(img, f"blur: {blur:.0f} (thr {self.blur_threshold:.0f})",
                    (8, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(img, hint, (8, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
        if capture_highlight:
            cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 255, 255), 4)

        return FrameResult(
            jpeg=encode_preview(img),
            width=frame_bgr.shape[1],
            height=frame_bgr.shape[0],
            board_found=board_found,
            corners=cornersL,
            corners_right=cornersR,
            blur_score=blur,
            capture_taken=capture_taken,
            capture_highlight=capture_highlight,
        )
