"""Run the final calibration on the captures saved for a session."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException

from ..core import calibration as calib_core
from ..core.storage import session_dir
from ..models.schemas import CalibrationResult, Chessboard, CameraMode
from .sessions import finish_session, get_session

router = APIRouter(prefix="/calibrate", tags=["calibrate"])


def _infer_image_size(sdir: Path) -> Tuple[int, int]:
    """Recover the image (width, height) from the first PNG under frames/.

    Falls back to (1280, 720) if no frames are available — calibration will
    still succeed against that size, just with slightly worse accuracy.
    """
    frames = sorted((sdir / "frames").glob("*.png"))
    if frames:
        img = cv2.imread(str(frames[0]))
        if img is not None:
            return int(img.shape[1]), int(img.shape[0])
    return 1280, 720


def _load_chessboard_for_session(sdir: Path) -> Chessboard:
    """Read the chessboard (stored under ``profile`` key in session.json) and
    normalise legacy fields (``stereo: true`` -> ``mode: stereo_lr``)."""
    data = json.loads((sdir / "session.json").read_text())
    cb = dict(data.get("profile") or {})
    if "mode" not in cb and cb.get("stereo"):
        cb["mode"] = CameraMode.STEREO_LR.value
    if "mode" not in cb:
        cb["mode"] = CameraMode.MONO.value
    return Chessboard(**cb)


@router.post("/{session_id}", response_model=CalibrationResult)
def run_calibration(session_id: str) -> CalibrationResult:
    info = get_session(session_id)
    if info.captures < 3:
        raise HTTPException(400, "Need at least 3 captures before calibrating")

    sdir = session_dir(session_id)
    chessboard = _load_chessboard_for_session(sdir)

    # image_size: prefer the metadata file written by the streaming layer.
    meta_file = sdir / "image_size.json"
    if meta_file.exists():
        image_size = tuple(json.loads(meta_file.read_text())["image_size"])
    else:
        image_size = _infer_image_size(sdir)
        meta_file.write_text(json.dumps({"image_size": list(image_size)}))

    try:
        if chessboard.is_stereo:
            ld = sdir / "corners_left"
            rd = sdir / "corners_right"
            lf = sorted(ld.glob("*.npy")) if ld.exists() else []
            rf = sorted(rd.glob("*.npy")) if rd.exists() else []
            if len(lf) < 3 or len(rf) < 3:
                raise HTTPException(400, "Not enough stereo corner pairs (need >= 3)")
            left = [np.load(p) for p in lf]
            right = [np.load(p) for p in rf]
            meta = calib_core.calibrate_stereo(
                profile=chessboard, image_size=image_size,
                left_captures=left, right_captures=right, out_dir=sdir,
            )
        else:
            corners_dir = sdir / "corners"
            npy_files = sorted(corners_dir.glob("*.npy")) if corners_dir.exists() else []
            if len(npy_files) < 3:
                raise HTTPException(400, "Not enough corner arrays")
            captures: List[np.ndarray] = [np.load(p) for p in npy_files]
            meta = calib_core.calibrate(
                profile=chessboard, image_size=image_size,
                captures=captures, out_dir=sdir,
            )
    except ValueError as exc:
        finish_session(session_id, state="failed")
        raise HTTPException(400, str(exc))

    finish_session(
        session_id,
        error=meta["reprojection_error"],
        rms=meta["rms"],
        result_files=[
            meta["files"]["npz"],
            meta["files"]["yaml"],
            meta["files"]["meta"],
        ],
    )

    return CalibrationResult(
        session_id=session_id,
        reprojection_error=meta["reprojection_error"],
        rms=meta["rms"],
        image_size=meta["image_size"],
        chessboard=chessboard,
        npz_path=meta["files"]["npz"],
        yaml_path=meta["files"]["yaml"],
        meta_path=meta["files"]["meta"],
    )


@router.get("/{session_id}/files")
def list_result_files(session_id: str):
    info = get_session(session_id)
    return {"files": info.result_files}


@router.get("/{session_id}/intrinsics")
def get_intrinsics(session_id: str) -> dict:
    """Return calibration intrinsics as a clean, human-readable YAML string."""
    sdir = session_dir(session_id)
    npz_path = sdir / "result.npz"
    if not npz_path.exists():
        raise HTTPException(404, "No calibration result — run calibration first")

    data = np.load(str(npz_path), allow_pickle=True)
    image_size = [int(x) for x in data["image_size"].tolist()]
    rms = float(data["rms"])

    try:
        meta = json.loads((sdir / "session.json").read_text())
        name = meta.get("name", session_id)
        n_captures = meta.get("captures", "?")
        created = meta.get("created_at", "")[:10]
    except Exception:
        name, n_captures, created = session_id, "?", ""

    coeff_labels = ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"]

    # Stereo result: render both cameras + relative pose.
    if "stereo" in data.files and bool(data["stereo"]):
        def _cam_block(K, dist, label):
            dl = "\n".join(
                f"    {coeff_labels[i] if i < len(coeff_labels) else f'c{i}'}: {v:.8f}"
                for i, v in enumerate(np.asarray(dist).flatten().tolist())
            )
            return (
                f"{label}:\n"
                f"  camera_matrix:\n"
                f"    fx: {K[0,0]:.8f}\n    fy: {K[1,1]:.8f}\n"
                f"    cx: {K[0,2]:.8f}\n    cy: {K[1,2]:.8f}\n"
                f"  dist_coeffs:\n{dl}\n"
            )
        R = np.asarray(data["R"]); T = np.asarray(data["T"]).flatten()
        baseline = float(data["baseline_mm"]) if "baseline_mm" in data.files else float(np.linalg.norm(T))
        R_rows = "\n".join("    - [" + ", ".join(f"{v:.8f}" for v in row) + "]" for row in R)
        yaml_str = (
            f"# Stereo calibration — {name}  ({created})\n"
            f"# Pairs: {n_captures}  |  Stereo RMS: {rms:.4f} px  |  Baseline: {baseline:.2f} mm\n\n"
            f"image_width: {image_size[0]}\nimage_height: {image_size[1]}\n\n"
            f"{_cam_block(np.asarray(data['K1']), data['D1'], 'left')}\n"
            f"{_cam_block(np.asarray(data['K2']), data['D2'], 'right')}\n"
            f"extrinsics:\n"
            f"  baseline_mm: {baseline:.4f}\n"
            f"  T_mm: [{T[0]:.6f}, {T[1]:.6f}, {T[2]:.6f}]\n"
            f"  R:\n{R_rows}\n"
        )
        return {"yaml": yaml_str, "name": name, "session_id": session_id}

    K = data["camera_matrix"]
    dist = data["dist_coeffs"].flatten()
    reproj = float(data["reprojection_error"])
    dist_lines = "\n".join(
        f"  {coeff_labels[i] if i < len(coeff_labels) else f'c{i}'}: {v:.8f}"
        for i, v in enumerate(dist.tolist())
    )

    yaml_str = (
        f"# Camera calibration — {name}  ({created})\n"
        f"# Captures: {n_captures}  |  RMS: {rms:.4f}  |  Reprojection error: {reproj:.4f} px\n"
        f"\n"
        f"image_width: {image_size[0]}\n"
        f"image_height: {image_size[1]}\n"
        f"\n"
        f"camera_matrix:\n"
        f"  fx: {K[0, 0]:.8f}\n"
        f"  fy: {K[1, 1]:.8f}\n"
        f"  cx: {K[0, 2]:.8f}\n"
        f"  cy: {K[1, 2]:.8f}\n"
        f"\n"
        f"dist_coeffs:\n"
        f"{dist_lines}\n"
    )

    return {"yaml": yaml_str, "name": name, "session_id": session_id}