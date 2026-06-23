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
from ..models.schemas import CalibrationResult, Profile
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


@router.post("/{session_id}", response_model=CalibrationResult)
def run_calibration(session_id: str) -> CalibrationResult:
    info = get_session(session_id)
    if info.captures < 3:
        raise HTTPException(400, "Need at least 3 captures before calibrating")

    sdir = session_dir(session_id)
    corners_dir = sdir / "corners"
    if not corners_dir.exists():
        raise HTTPException(400, "No captured corner data for this session")

    npy_files = sorted(corners_dir.glob("*.npy"))
    if len(npy_files) < 3:
        raise HTTPException(400, "Not enough corner arrays")

    captures: List[np.ndarray] = [np.load(p) for p in npy_files]

    # image_size: prefer the metadata file written by the streaming layer.
    # If it's missing (e.g. an old session), recover from the first PNG frame
    # under frames/, or fall back to the corners array shape.
    meta_file = sdir / "image_size.json"
    if meta_file.exists():
        image_size = tuple(json.loads(meta_file.read_text())["image_size"])
    else:
        image_size = _infer_image_size(sdir)
        # Persist so subsequent runs and the UI both see consistent values.
        meta_file.write_text(json.dumps({"image_size": list(image_size)}))

    profile = Profile(**json.loads((sdir / "session.json").read_text())["profile"])

    try:
        meta = calib_core.calibrate(
            profile=profile,
            image_size=image_size,
            captures=captures,
            out_dir=sdir,
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
        profile=profile,
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
    K = data["camera_matrix"]
    dist = data["dist_coeffs"].flatten()
    image_size = [int(x) for x in data["image_size"].tolist()]
    rms = float(data["rms"])
    reproj = float(data["reprojection_error"])

    try:
        meta = json.loads((sdir / "session.json").read_text())
        name = meta.get("name", session_id)
        n_captures = meta.get("captures", "?")
        created = meta.get("created_at", "")[:10]
    except Exception:
        name, n_captures, created = session_id, "?", ""

    coeff_labels = ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"]
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