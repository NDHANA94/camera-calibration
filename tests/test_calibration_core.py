"""Unit tests for `server.core.calibration` (mono + stereo + flag plumbing)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from server.core import calibration as calib_core
from server.models.schemas import CalibrationFlags, CameraMode, Chessboard
from tests._synthetic import (
    default_synthetic_camera,
    generate_capture_set,
    generate_stereo_pair,
    relativize_pose,
)


# Tolerances for "recovered matches ground truth" checks.
# Synth corners get a 0.3 px noise injection so RMS isn't unrealistically low.
_REL_K_TOL = 0.05       # 5 % relative error per K element
_REL_FOCAL_TOL = 0.03   # 3 % on focal length (often the most-constrained)
_RMS_MONO = 0.7
_RMS_STEREO = 1.0


def _chessboard(cam, **overrides) -> Chessboard:
    return Chessboard(
        name="test",
        inner_corners_x=cam.board_w,
        inner_corners_y=cam.board_h,
        square_size_mm=cam.square_size_mm,
        mode=CameraMode.MONO,
        **overrides,
    )


def _chessboard_stereo(cam, **overrides) -> Chessboard:
    return Chessboard(
        name="test_stereo",
        inner_corners_x=cam.board_w,
        inner_corners_y=cam.board_h,
        square_size_mm=cam.square_size_mm,
        mode=CameraMode.STEREO_LR,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Mono calibration
# ---------------------------------------------------------------------------

def test_calibrate_recovers_K_within_tolerance(tmp_path: Path, synthetic_cam):
    """Run mono calibration on synthetic captures; K should match ground truth."""
    captures = generate_capture_set(synthetic_cam, n_captures=15, seed=42)
    assert len(captures) >= 10, "synthetic pose generator should yield enough diverse poses"

    cb = _chessboard(synthetic_cam)
    objp = calib_core._object_points(cb.inner_corners_x, cb.inner_corners_y, cb.square_size_mm)
    obj_points = [objp for _ in captures]
    img_points = [c[0].reshape(-1, 1, 2).astype(np.float32) for c in captures]

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points, img_points, synthetic_cam.image_size, None, None,
        flags=int(cb.flags),
    )

    # RMS should be in the synth-noise regime (≤ 0.7 px with 0.3 px sigma)
    assert rms < _RMS_MONO, f"RMS {rms:.3f} exceeded {_RMS_MONO}"

    # Focal lengths should be within 3 % of ground truth
    assert K[0, 0] == pytest.approx(synthetic_cam.K[0, 0], rel=_REL_FOCAL_TOL)
    assert K[1, 1] == pytest.approx(synthetic_cam.K[1, 1], rel=_REL_FOCAL_TOL)

    # Principal point within 5 % of image center
    cx_gt, cy_gt = synthetic_cam.K[0, 2], synthetic_cam.K[1, 2]
    assert abs(K[0, 2] - cx_gt) / synthetic_cam.image_size[0] < 0.05
    assert abs(K[1, 2] - cy_gt) / synthetic_cam.image_size[1] < 0.05


def test_calibrate_writes_npz_yaml_and_meta(tmp_path: Path, synthetic_cam):
    """End-to-end: call `calibrate.calibrate()` and inspect the artifacts."""
    captures = generate_capture_set(synthetic_cam, n_captures=10, seed=99)
    cb = _chessboard(synthetic_cam)

    meta = calib_core.calibrate(
        profile=cb,
        image_size=synthetic_cam.image_size,
        captures=[c[0] for c in captures],
        out_dir=tmp_path,
    )

    # Files exist
    npz_path = Path(meta["files"]["npz"])
    yaml_path = Path(meta["files"]["yaml"])
    meta_path = Path(meta["files"]["meta"])
    assert npz_path.exists()
    assert yaml_path.exists()
    assert meta_path.exists()

    # npz contents
    data = np.load(str(npz_path), allow_pickle=True)
    assert "camera_matrix" in data.files
    assert "dist_coeffs" in data.files
    assert "image_size" in data.files
    assert "rms" in data.files
    assert "profile_inner_corners_x" in data.files
    assert data["profile_inner_corners_x"] == synthetic_cam.board_w

    # yaml round-trip via cv2.FileStorage
    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_READ)
    try:
        assert fs.getNode("camera_matrix").mat() is not None
        assert fs.getNode("distortion_coefficients").mat() is not None
        assert fs.getNode("image_width").real() == synthetic_cam.image_size[0]
        assert fs.getNode("image_height").real() == synthetic_cam.image_size[1]
        assert fs.getNode("rms").real() > 0
        assert fs.getNode("board_width").real() == synthetic_cam.board_w
        assert fs.getNode("board_height").real() == synthetic_cam.board_h
        assert fs.getNode("square_size_mm").real() == pytest.approx(synthetic_cam.square_size_mm)
        # per-capture rvec_0 / tvec_0 should exist
        assert fs.getNode("rvec_0").mat() is not None
        assert fs.getNode("tvec_0").mat() is not None
    finally:
        fs.release()

    # meta.json round-trip
    import json
    meta_dict = json.loads(meta_path.read_text())
    assert meta_dict["n_captures"] == 10
    assert meta_dict["image_size"] == list(synthetic_cam.image_size)
    assert meta_dict["profile"]["inner_corners_x"] == synthetic_cam.board_w


def test_calibrate_rejects_too_few_captures(tmp_path: Path, synthetic_cam):
    """Calibration must error out on < 3 captures."""
    cb = _chessboard(synthetic_cam)
    with pytest.raises(ValueError, match="at least 3"):
        calib_core.calibrate(
            profile=cb,
            image_size=synthetic_cam.image_size,
            captures=[np.zeros((synthetic_cam.num_corners, 1, 2), dtype=np.float32)],
            out_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Reprojection error helper
# ---------------------------------------------------------------------------

def test_reprojection_error_near_zero_on_known_geometry(synthetic_cam):
    """`_reprojection_error` should be ~0 when re-projecting the same K/dist/rvec/tvec."""
    captures = generate_capture_set(synthetic_cam, n_captures=5, seed=1)
    cb = _chessboard(synthetic_cam)
    objp = calib_core._object_points(cb.inner_corners_x, cb.inner_corners_y, cb.square_size_mm)
    obj_points = [objp for _ in captures]
    img_points = [c[0].reshape(-1, 1, 2).astype(np.float32) for c in captures]
    rvecs = [c[1] for c in captures]
    tvecs = [c[2] for c in captures]

    err = calib_core._reprojection_error(
        obj_points, img_points, rvecs, tvecs, synthetic_cam.K, synthetic_cam.dist,
    )
    # With 0.3 px injected noise, mean reprojection should be ~0.3 px
    assert err < 0.5, f"unexpectedly high reprojection: {err}"


# ---------------------------------------------------------------------------
# Calibration flags plumbing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag_value", [
    CalibrationFlags.NONE,
    CalibrationFlags.FIX_ASPECT_RATIO,
    CalibrationFlags.ZERO_TANGENT_DIST,
    CalibrationFlags.RATIONAL_MODEL,
    CalibrationFlags.FIX_ASPECT_RATIO | CalibrationFlags.ZERO_TANGENT_DIST,
    CalibrationFlags.FIX_ASPECT_RATIO | CalibrationFlags.ZERO_TANGENT_DIST | CalibrationFlags.RATIONAL_MODEL,
])
def test_calibration_flags_plumbing(tmp_path: Path, synthetic_cam, flag_value: int):
    """Each flag combination should run without error and produce all artifacts."""
    captures = generate_capture_set(synthetic_cam, n_captures=12, seed=11)
    cb = _chessboard(synthetic_cam, flags=int(flag_value))
    meta = calib_core.calibrate(
        profile=cb,
        image_size=synthetic_cam.image_size,
        captures=[c[0] for c in captures],
        out_dir=tmp_path,
    )
    assert Path(meta["files"]["npz"]).exists()
    assert Path(meta["files"]["yaml"]).exists()
    assert Path(meta["files"]["meta"]).exists()
    # RMS must still be sane with the flag combination
    assert meta["reprojection_error"] < 5.0


# ---------------------------------------------------------------------------
# Stereo calibration
# ---------------------------------------------------------------------------

def test_stereo_calibration_recovers_baseline(tmp_path: Path, synthetic_cam):
    """Stereo calibration should recover the baseline within 5 %."""
    baseline_mm = 80.0
    left, right, rv_l, tv_l, rv_r, tv_r = generate_stereo_pair(
        synthetic_cam, baseline_mm=baseline_mm, n_pairs=12, seed=3,
    )
    assert len(left) >= 10

    cb = _chessboard_stereo(synthetic_cam)

    meta = calib_core.calibrate_stereo(
        profile=cb,
        image_size=synthetic_cam.image_size,
        left_captures=left,
        right_captures=right,
        out_dir=tmp_path,
    )

    # Baseline within 5 % (synthetic noise tolerance)
    assert meta["baseline_mm"] == pytest.approx(baseline_mm, rel=0.05)
    # Stereo RMS should be in the noise regime
    assert meta["rms"] < _RMS_STEREO

    # Artifacts
    data = np.load(str(meta["files"]["npz"]), allow_pickle=True)
    assert bool(data["stereo"])
    assert "K1" in data.files
    assert "K2" in data.files
    assert "R" in data.files
    assert "T" in data.files
    assert "Q" in data.files
    # Per-eye intrinsics should still match ground truth
    K1 = data["K1"]
    K2 = data["K2"]
    assert K1[0, 0] == pytest.approx(synthetic_cam.K[0, 0], rel=_REL_FOCAL_TOL + 0.02)
    assert K2[0, 0] == pytest.approx(synthetic_cam.K[0, 0], rel=_REL_FOCAL_TOL + 0.02)


def test_stereo_calibration_writes_yaml_with_both_blocks(tmp_path: Path, synthetic_cam):
    """The stereo YAML should expose K1, D1, K2, D2, R, T, baseline_mm."""
    left, right, *_ = generate_stereo_pair(synthetic_cam, n_pairs=8, seed=5)
    cb = _chessboard_stereo(synthetic_cam)
    meta = calib_core.calibrate_stereo(
        profile=cb,
        image_size=synthetic_cam.image_size,
        left_captures=left,
        right_captures=right,
        out_dir=tmp_path,
    )
    fs = cv2.FileStorage(str(meta["files"]["yaml"]), cv2.FILE_STORAGE_READ)
    try:
        assert fs.getNode("stereo").real() == 1.0
        for key in ("K1", "D1", "K2", "D2", "R", "T", "rms", "baseline_mm",
                    "image_width", "image_height"):
            node = fs.getNode(key)
            assert not node.empty(), f"missing key {key} in stereo yaml"
    finally:
        fs.release()


def test_stereo_rejects_too_few_pairs(tmp_path: Path, synthetic_cam):
    cb = _chessboard_stereo(synthetic_cam)
    with pytest.raises(ValueError, match="at least 3"):
        calib_core.calibrate_stereo(
            profile=cb,
            image_size=synthetic_cam.image_size,
            left_captures=[np.zeros((synthetic_cam.num_corners, 1, 2), dtype=np.float32)],
            right_captures=[np.zeros((synthetic_cam.num_corners, 1, 2), dtype=np.float32)],
            out_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Object-point helper
# ---------------------------------------------------------------------------

def test_object_points_shape_and_scale():
    """`_object_points` returns (W*H, 3) with x/y in millimetres scaled by square_size."""
    objp = calib_core._object_points(8, 5, 30.0)
    assert objp.shape == (40, 3)
    # z column is zero
    assert np.all(objp[:, 2] == 0)
    # x and y are scaled
    assert objp[-1, 0] == pytest.approx(7 * 30.0)
    assert objp[-1, 1] == pytest.approx(4 * 30.0)


# ---------------------------------------------------------------------------
# Edge cases on the synthetic generator
# ---------------------------------------------------------------------------

def test_synthetic_pose_generator_produces_diverse_poses(synthetic_cam):
    captures = generate_capture_set(synthetic_cam, n_captures=15, seed=42)
    # Pairwise angle check
    axes = []
    for c in captures:
        R, _ = cv2.Rodrigues(c[1])
        axes.append(R[:, 2])
    for i in range(len(axes)):
        for j in range(i + 1, len(axes)):
            ang = float(np.degrees(np.arccos(np.clip(abs(float(axes[i] @ axes[j])), -1.0, 1.0))))
            assert ang >= 11.0, f"poses {i} and {j} too close: {ang:.1f}°"