"""Pose-coverage guidance: tells the user when their captured board
orientations look too similar, so they actually span the view frustum.

We compare board poses by angle between the dominant axis of the detected
corners' plane normal in each capture. Crude but effective for prompting
"tilt the board more".
"""
from __future__ import annotations

from typing import List, Sequence

import numpy as np


def _pose_axis(corners: np.ndarray) -> np.ndarray:
    """Best-effort: return the normal of the plane spanned by the inner corners.

    `corners` is shape (N, 1, 2) from cv2.findChessboardCorners. We SVD on the
    3D-ish projection (x, y, 0) so it always works in image space.
    """
    pts = corners.reshape(-1, 2).astype(np.float32)
    if len(pts) < 3:
        return np.array([0.0, 0.0, 1.0])
    center = pts.mean(axis=0)
    centered = pts - center
    # Add a third zero column so we can compute a meaningful "normal".
    pts3 = np.column_stack([centered, np.zeros(len(centered))])
    _, _, vh = np.linalg.svd(pts3, full_matrices=False)
    return vh[-1]


def _angle(a: np.ndarray, b: np.ndarray) -> float:
    a_n = a / (np.linalg.norm(a) + 1e-9)
    b_n = b / (np.linalg.norm(b) + 1e-9)
    cos = float(np.clip(np.dot(a_n, b_n), -1.0, 1.0))
    return float(np.degrees(np.arccos(cos)))


def coverage_tip(captures: Sequence[np.ndarray]) -> str:
    """Return a short hint string for the UI.

    Strategy: if every captured pose is within `min_angle` of every other one,
    we tell the user to tilt the board. Otherwise encourage more views.
    """
    if len(captures) < 2:
        return "Hold the board flat in view. Hold steady to auto-capture."
    axes = [_pose_axis(c) for c in captures]
    min_angle = min(_angle(axes[i], axes[j]) for i in range(len(axes)) for j in range(i + 1, len(axes)))
    if min_angle < 8.0:
        return "Captures are very similar — tilt the board ~30° and try again."
    if min_angle < 20.0:
        return "Vary the board angle more for a better calibration."
    if len(captures) < 8:
        return "Good coverage. Capture a few more views."
    return "Coverage looks diverse. You can finish when ready."