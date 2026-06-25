"""Helpers for generating synthetic calibration data.

Used by unit + API integration tests to produce known ground-truth camera
matrices, distortion coefficients, and corner arrays so tests can compare
recovered intrinsics against ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


@dataclass
class SyntheticCamera:
    """A known camera + chessboard setup, used to generate test data."""

    K: np.ndarray              # 3x3 camera matrix
    dist: np.ndarray           # distortion coefficients (k1, k2, p1, p2, k3, ...)
    image_size: Tuple[int, int]  # (width, height)
    board_w: int               # inner corners along x
    board_h: int               # inner corners along y
    square_size_mm: float      # physical square size in mm

    @property
    def num_corners(self) -> int:
        return self.board_w * self.board_h


def default_synthetic_camera(
    image_size: Tuple[int, int] = (1280, 720),
    board_w: int = 8,
    board_h: int = 5,
    square_size_mm: float = 30.0,
    distortion: bool = True,
) -> SyntheticCamera:
    """Return a sensible mid-range synthetic camera (≈ 70° HFOV)."""
    w, h = image_size
    # Focal length that gives ~70° horizontal FOV
    fx = fy = float(w) / (2.0 * np.tan(np.deg2rad(35.0)))
    cx, cy = w / 2.0, h / 2.0
    K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
    if distortion:
        dist = np.array([-0.20, 0.05, 0.001, -0.002, 0.0], dtype=np.float64)
    else:
        dist = np.zeros(5, dtype=np.float64)
    return SyntheticCamera(
        K=K, dist=dist, image_size=image_size,
        board_w=board_w, board_h=board_h, square_size_mm=square_size_mm,
    )


def render_chessboard(
    cam: SyntheticCamera,
    image: Optional[np.ndarray] = None,
    board_origin: Tuple[int, int] = (50, 50),
    square_px: int = 60,
) -> np.ndarray:
    """Render a real checkerboard pattern (alternating black/white squares).

    `cv2.findChessboardCorners` needs an actual alternating pattern, not just
    a grid of lines. Returns the rendered image so callers can detect corners.

    NOTE: We use numpy slicing rather than `cv2.rectangle` here because
    `cv2.rectangle(..., thickness=-1)` produces a buffer that
    `cv2.findChessboardCorners` rejects (OpenCV's fast-check path treats it
    as noise). Slicing keeps the array contiguous and unambiguous.
    """
    h_img, w_img = cam.image_size[1], cam.image_size[0]
    if image is None:
        image = np.full((h_img, w_img, 3), 255, dtype=np.uint8)
    x0, y0 = board_origin
    cols, rows = cam.board_w + 1, cam.board_h + 1
    for j in range(rows):
        for i in range(cols):
            xs = x0 + i * square_px
            ys = y0 + j * square_px
            color = (0, 0, 0) if (i + j) % 2 == 0 else (255, 255, 255)
            image[ys:ys + square_px, xs:xs + square_px] = color
    return image


def random_pose(
    rng: np.random.Generator,
    min_angle_deg: float = 12.0,
    max_tilt_deg: float = 35.0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate a random but diverse camera pose (rvec, tvec).

    `min_angle_deg` is enforced between consecutive poses when generating a
    *sequence*; callers iterating `n_captures` times should accumulate the
    chosen angles and pick a new one if too close to any existing axis.
    """
    # Tilt the board within a reasonable range
    rx = np.deg2rad(rng.uniform(-max_tilt_deg, max_tilt_deg))
    ry = np.deg2rad(rng.uniform(-max_tilt_deg, max_tilt_deg))
    rz = np.deg2rad(rng.uniform(-10.0, 10.0))
    rvec = np.array([rx, ry, rz], dtype=np.float64).reshape(3, 1)
    # Translation: ~400–800 mm away along +Z, plus a small lateral offset
    tvec = np.array(
        [rng.uniform(-60, 60), rng.uniform(-40, 40), rng.uniform(400, 800)],
        dtype=np.float64,
    ).reshape(3, 1)
    return rvec, tvec


def project_chessboard_corners(
    cam: SyntheticCamera,
    rvec: np.ndarray,
    tvec: np.ndarray,
    *,
    noise_sigma_px: float = 0.3,
    rng: Optional[np.random.Generator] = None,
) -> np.ndarray:
    """Project the chessboard's inner corners through `cam.K` + `cam.dist`.

    Returns corners of shape `(board_w * board_h, 1, 2)` suitable for
    `cv2.calibrateCamera` / saving to `corners/NNNN.npy`.
    """
    if rng is None:
        rng = np.random.default_rng(0)
    # Object points in mm, shape (N, 3)
    objp = np.zeros((cam.num_corners, 3), dtype=np.float32)
    grid = np.mgrid[0:cam.board_w, 0:cam.board_h].T.reshape(-1, 2)
    objp[:, :2] = grid * cam.square_size_mm
    # Project
    img_pts, _ = cv2.projectPoints(objp, rvec, tvec, cam.K, cam.dist)
    # Add a small amount of sub-pixel noise so RMS isn't unrealistically low
    if noise_sigma_px > 0:
        img_pts = img_pts + rng.normal(0.0, noise_sigma_px, img_pts.shape).astype(np.float32)
    return img_pts.astype(np.float32)


def generate_capture_set(
    cam: SyntheticCamera,
    n_captures: int = 15,
    *,
    min_angle_deg: float = 12.0,
    seed: int = 42,
) -> List[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Return a list of (corners, rvec, tvec) tuples with diverse poses.

    Enforces a minimum angle between any two board pose axes so
    `cv2.calibrateCamera` converges reliably.
    """
    rng = np.random.default_rng(seed)
    captures: List[Tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    axes: List[np.ndarray] = []
    attempts = 0
    while len(captures) < n_captures and attempts < n_captures * 20:
        attempts += 1
        rvec, tvec = random_pose(rng)
        # Board axis = -Z column of the rotation matrix (camera looks down -Z
        # so the board faces back along +Z; just use the rotation's last col).
        R, _ = cv2.Rodrigues(rvec)
        axis = R[:, 2]
        if axes:
            min_ang = min(
                float(np.degrees(np.arccos(np.clip(abs(float(a @ axis)), -1.0, 1.0))))
                for a in axes
            )
            if min_ang < min_angle_deg:
                continue
        corners = project_chessboard_corners(cam, rvec, tvec, rng=rng)
        captures.append((corners, rvec, tvec))
        axes.append(axis)
    return captures


def generate_stereo_pair(
    cam: SyntheticCamera,
    baseline_mm: float = 60.0,
    n_pairs: int = 12,
    seed: int = 7,
) -> Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray], List[np.ndarray], List[np.ndarray], List[np.ndarray]]:
    """Generate paired left/right corner sets plus their rvec/tvec.

    Right camera is translated by `+baseline_mm` along world X and rotated to
    look at the same board — for simplicity here we just translate, no extra
    rotation (the calibration code handles that downstream).
    """
    rng = np.random.default_rng(seed)
    left_pts: List[np.ndarray] = []
    right_pts: List[np.ndarray] = []
    rv_l: List[np.ndarray] = []
    tv_l: List[np.ndarray] = []
    rv_r: List[np.ndarray] = []
    tv_r: List[np.ndarray] = []
    axes: List[np.ndarray] = []
    attempts = 0
    while len(left_pts) < n_pairs and attempts < n_pairs * 20:
        attempts += 1
        rvec, tvec = random_pose(rng)
        R, _ = cv2.Rodrigues(rvec)
        axis = R[:, 2]
        if axes:
            min_ang = min(
                float(np.degrees(np.arccos(np.clip(abs(float(a @ axis)), -1.0, 1.0))))
                for a in axes
            )
            if min_ang < 12.0:
                continue
        l_corners = project_chessboard_corners(cam, rvec, tvec, rng=rng)
        # Right camera: same rotation, translation offset by baseline on X
        tvec_r = tvec.copy().reshape(3)
        tvec_r[0] += baseline_mm
        r_corners = project_chessboard_corners(cam, rvec, tvec_r.reshape(3, 1), rng=rng)
        left_pts.append(l_corners)
        right_pts.append(r_corners)
        rv_l.append(rvec)
        tv_l.append(tvec)
        rv_r.append(rvec.copy())
        tv_r.append(tvec_r.reshape(3, 1))
        axes.append(axis)
    return left_pts, right_pts, rv_l, tv_l, rv_r, tv_r


def relativize_pose(rv_a: np.ndarray, tv_a: np.ndarray, rv_b: np.ndarray, tv_b: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Compute (R, T) such that b's pose expressed in a's frame.

    R = R_a^T · R_b
    T = R_a^T · (t_b - t_a)
    """
    Ra, _ = cv2.Rodrigues(rv_a)
    Rb, _ = cv2.Rodrigues(rv_b)
    R = Ra.T @ Rb
    T = Ra.T @ (tv_b.flatten() - tv_a.flatten())
    return R, T.reshape(3, 1)