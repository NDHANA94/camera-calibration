"""Calibration: build object points, run cv2.calibrateCamera, save results
in both NumPy (.npz) and OpenCV YAML (.yaml) formats plus a JSON sidecar
with metadata.

Per the planning decision, results are saved in BOTH formats so consumers
can pick whichever they prefer.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Sequence, Tuple

import cv2
import numpy as np

from ..models.schemas import Profile

log = logging.getLogger(__name__)


def _object_points(board_w: int, board_h: int, square_size_mm: float) -> np.ndarray:
    """Standard object-point grid in mm. Shape (N, 3) for a single view."""
    objp = np.zeros((board_w * board_h, 3), np.float32)
    objp[:, :2] = np.mgrid[0:board_w, 0:board_h].T.reshape(-1, 2)
    return objp * float(square_size_mm)


def calibrate(
    profile: Profile,
    image_size: Tuple[int, int],
    captures: Sequence[np.ndarray],
    out_dir: Path,
) -> dict:
    """Run cv2.calibrateCamera and write result files. Returns the metadata dict.

    `captures` is a list of (N, 1, 2) corner arrays as produced by the pipeline.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    objp = _object_points(profile.inner_corners_x, profile.inner_corners_y, profile.square_size_mm)

    obj_points: List[np.ndarray] = []
    img_points: List[np.ndarray] = []
    for corners in captures:
        if corners is None:
            continue
        obj_points.append(objp)
        img_points.append(corners.reshape(-1, 1, 2).astype(np.float32))

    if len(obj_points) < 3:
        raise ValueError(
            f"Need at least 3 valid captures for calibration; got {len(obj_points)}"
        )

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_points,
        img_points,
        image_size,
        None,
        None,
        flags=int(profile.flags),
    )

    mean_err = _reprojection_error(obj_points, img_points, rvecs, tvecs, K, dist)

    npz_path = out_dir / "result.npz"
    yaml_path = out_dir / "result.yaml"
    meta_path = out_dir / "meta.json"

    np.savez(
        npz_path,
        camera_matrix=K,
        dist_coeffs=dist,
        rvecs=np.array(rvecs, dtype=object),
        tvecs=np.array(tvecs, dtype=object),
        image_size=np.array(image_size),
        rms=np.array(rms),
        reprojection_error=np.array(mean_err),
        profile_inner_corners_x=np.array(profile.inner_corners_x),
        profile_inner_corners_y=np.array(profile.inner_corners_y),
        profile_square_size_mm=np.array(profile.square_size_mm),
    )

    fs = cv2.FileStorage(str(yaml_path), cv2.FILE_STORAGE_WRITE)
    fs.write("camera_matrix", K)
    fs.write("distortion_coefficients", dist)
    fs.write("image_width", image_size[0])
    fs.write("image_height", image_size[1])
    fs.write("rms", float(rms))
    fs.write("reprojection_error", float(mean_err))
    fs.write("square_size_mm", float(profile.square_size_mm))
    fs.write("board_width", int(profile.inner_corners_x))
    fs.write("board_height", int(profile.inner_corners_y))
    for i, (rv, tv) in enumerate(zip(rvecs, tvecs)):
        fs.write(f"rvec_{i}", rv)
        fs.write(f"tvec_{i}", tv)
    fs.release()

    meta = {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "image_size": list(image_size),
        "rms": float(rms),
        "reprojection_error": float(mean_err),
        "n_captures": len(obj_points),
        "profile": {
            "inner_corners_x": profile.inner_corners_x,
            "inner_corners_y": profile.inner_corners_y,
            "square_size_mm": profile.square_size_mm,
            "flags": int(profile.flags),
        },
        "files": {
            "npz": str(npz_path),
            "yaml": str(yaml_path),
            "meta": str(meta_path),
        },
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    log.info(
        "Calibration done: rms=%.4f reproj=%.4f captures=%d",
        rms,
        mean_err,
        len(obj_points),
    )
    return meta


def _reprojection_error(obj_points, img_points, rvecs, tvecs, K, dist) -> float:
    total = 0.0
    n = 0
    for op, ip, rv, tv in zip(obj_points, img_points, rvecs, tvecs):
        proj, _ = cv2.projectPoints(op, rv, tv, K, dist)
        err = cv2.norm(ip, proj, cv2.NORM_L2) / len(proj)
        total += err
        n += 1
    return total / max(n, 1)