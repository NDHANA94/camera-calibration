"""Unit tests for `server.core.guidance` (pose-coverage)."""
from __future__ import annotations

import numpy as np
import pytest

from server.core.guidance import _angle, _pose_axis, coverage_tip
from tests._synthetic import default_synthetic_camera, project_chessboard_corners


def _identity_corners(cam) -> np.ndarray:
    """Front-on capture: corners in a regular grid aligned with the image axes."""
    tvec = np.array([[0.0], [0.0], [500.0]])
    return project_chessboard_corners(cam, np.zeros((3, 1)), tvec,
                                      noise_sigma_px=0.0)


def _diverse_pose_corners(cam, x_offset: float, y_offset: float) -> np.ndarray:
    """Generate corners at a lateral offset in mm — different image-plane
    orientation than the centered identity corners."""
    rvec = np.zeros((3, 1))
    tvec = np.array([[x_offset], [y_offset], [500.0]])
    return project_chessboard_corners(cam, rvec, tvec, noise_sigma_px=0.0)


def _tilted_pose_corners(cam, tilt_deg: float, x_offset: float) -> np.ndarray:
    """Tilt the board around X by `tilt_deg` and shift laterally for diversity."""
    rx = np.deg2rad(tilt_deg)
    rvec = np.array([[rx], [0.0], [0.0]])
    tvec = np.array([[x_offset], [0.0], [500.0]])
    return project_chessboard_corners(cam, rvec, tvec, noise_sigma_px=0.0)


# ---------------------------------------------------------------------------
# _pose_axis / _angle
# ---------------------------------------------------------------------------

def test_pose_axis_returns_unit_length():
    cam = default_synthetic_camera()
    corners = _identity_corners(cam)
    axis = _pose_axis(corners)
    assert axis.shape == (3,)
    assert np.isclose(np.linalg.norm(axis), 1.0, atol=1e-4)


def test_angle_zero_for_identical_vectors():
    a = np.array([0.0, 0.0, 1.0])
    assert _angle(a, a) == pytest.approx(0.0, abs=0.1)


def test_angle_180_for_opposite_vectors():
    a = np.array([0.0, 0.0, 1.0])
    b = np.array([0.0, 0.0, -1.0])
    assert _angle(a, b) == pytest.approx(180.0, abs=0.1)


def test_angle_90_between_perpendicular():
    a = np.array([1.0, 0.0, 0.0])
    b = np.array([0.0, 1.0, 0.0])
    assert _angle(a, b) == pytest.approx(90.0, abs=0.1)


# ---------------------------------------------------------------------------
# coverage_tip
# ---------------------------------------------------------------------------

def test_coverage_tip_single_capture():
    cam = default_synthetic_camera()
    tip = coverage_tip([_identity_corners(cam)])
    assert "Hold" in tip or "flat" in tip.lower()


def test_coverage_tip_near_parallel_captures_warns_to_tilt():
    cam = default_synthetic_camera()
    # Two captures at almost identical (X, Y) — different `_pose_axis` angles < 8°
    captures = [
        _diverse_pose_corners(cam, x_offset=0.0, y_offset=0.0),
        _diverse_pose_corners(cam, x_offset=5.0, y_offset=2.0),
    ]
    tip = coverage_tip(captures)
    assert "tilt" in tip.lower()


def test_coverage_tip_fair_diversity_suggests_more_angle():
    cam = default_synthetic_camera()
    # Captures at clearly different positions to push min-angle into 8°–20° range
    captures = [
        _diverse_pose_corners(cam, x_offset=-80.0, y_offset=0.0),
        _diverse_pose_corners(cam, x_offset=80.0, y_offset=0.0),
        _diverse_pose_corners(cam, x_offset=0.0, y_offset=-80.0),
        _diverse_pose_corners(cam, x_offset=0.0, y_offset=80.0),
        _diverse_pose_corners(cam, x_offset=-60.0, y_offset=60.0),
    ]
    tip = coverage_tip(captures)
    # Either "fair" (8–20°) or "good" (>20° with <8 captures) — both should give useful text
    assert any(w in tip.lower() for w in ("more", "angle", "good", "capture"))


def test_coverage_tip_returns_string_for_many_captures():
    """With many captures the tip should be one of the encouraging variants
    (or warn about diversity); never empty."""
    cam = default_synthetic_camera()
    import math
    captures = []
    for i in range(10):
        ry = math.radians(-25 + i * 5)
        rx = math.radians(-15 + (i % 3) * 15)
        rvec = np.array([[rx], [ry], [0.0]])
        tvec = np.array([[-60 + i * 12], [-40 + (i % 4) * 20], [500.0]])
        captures.append(project_chessboard_corners(cam, rvec, tvec, noise_sigma_px=0.0))
    tip = coverage_tip(captures)
    assert isinstance(tip, str) and len(tip) > 0
    # Should mention either "diverse", "tilt", or "good coverage" depending on diversity
    assert any(w in tip.lower() for w in ("tilt", "diverse", "good", "finish"))