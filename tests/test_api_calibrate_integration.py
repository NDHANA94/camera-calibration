"""End-to-end API integration test for /calibrate.

This is the key validation layer: it boots the FastAPI app, creates a session,
injects synthetic corner arrays into the session directory (simulating what
the streaming layer would write after capturing frames), runs
`POST /calibrate/{id}`, and then validates the round-trip artifacts:

- `result.npz`  — np.load round-trips, K/dist/rvecs/tvecs/image_size present
- `result.yaml` — cv2.FileStorage reads back, expected keys present
- `meta.json`   — JSON shape is correct
- `GET /calibrate/{id}/intrinsics` returns YAML that parses cleanly

It also asserts the recovered intrinsics are within tolerance of ground truth.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from tests._synthetic import (
    default_synthetic_camera,
    generate_capture_set,
    generate_stereo_pair,
)


def _chessboard_payload(name: str = "test_cb", mode: str = "mono", **overrides) -> dict:
    base = {
        "name": name,
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "flags": 0,
        "required_captures": 10,
        "mode": mode,
    }
    base.update(overrides)
    return base


def _create_session(http_client, *, mode: str = "mono", name: str = "ci_test") -> str:
    payload = {
        "name": name,
        "source": "local",
        "camera_id": "/dev/video0",
        "chessboard": _chessboard_payload(name=f"cb_{name}", mode=mode),
    }
    if mode == "stereo_separate":
        payload["camera_id_2"] = "/dev/video1"
    resp = http_client.post("/sessions", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _seed_corners(sdir: Path, cam, n_captures: int = 15, seed: int = 42):
    """Inject synthetic corner arrays + matching PNGs into the session directory.

    This mimics what the streaming layer would write during a real capture:
    - corners/NNNN.npy for mono / stereo_lr
    - corners_left/NNNN.npy + corners_right/NNNN.npy for stereo_separate
    - frames/NNNN.png so `_infer_image_size` can recover the dimensions
    - image_size.json so calibration has the per-eye/per-stream size ready
    - bumps `captures` in session.json (the calibrate route checks this)
    """
    captures = generate_capture_set(cam, n_captures=n_captures, seed=seed)
    assert len(captures) >= 10

    corners_dir = sdir / "corners"
    frames_dir = sdir / "frames"
    corners_dir.mkdir(exist_ok=True)
    frames_dir.mkdir(exist_ok=True)

    for idx, (corners, _rvec, _tvec) in enumerate(captures):
        np.save(corners_dir / f"{idx:04d}.npy", corners)
        # 1×1 PNG so imread returns the correct image_size
        w, h = cam.image_size
        png = np.full((h, w, 3), 200, dtype=np.uint8)
        cv2.imwrite(str(frames_dir / f"{idx:04d}.png"), png)

    # Persist image_size so the calibrate route doesn't need to re-infer
    (sdir / "image_size.json").write_text(json.dumps({
        "image_size": list(cam.image_size), "stereo": False,
    }))
    # Bump session.json's `captures` count so the route's `info.captures < 3`
    # guard passes (it reads from session.json, not the disk).
    session_json = sdir / "session.json"
    data = json.loads(session_json.read_text())
    data["captures"] = len(captures)
    session_json.write_text(json.dumps(data, indent=2))
    # Clear the in-memory session index so the route reloads from disk on the next
    # request — otherwise it uses the stale value from session creation.
    from server.routes import sessions as sessions_mod
    sessions_mod._INDEX.clear()
    return captures


def _seed_stereo_corners(sdir: Path, cam, baseline_mm: float = 60.0,
                         n_pairs: int = 12, seed: int = 7):
    left, right, *_ = generate_stereo_pair(
        cam, baseline_mm=baseline_mm, n_pairs=n_pairs, seed=seed,
    )
    assert len(left) >= 10
    left_dir = sdir / "corners_left"
    right_dir = sdir / "corners_right"
    frames_dir = sdir / "frames"
    for d in (left_dir, right_dir, frames_dir):
        d.mkdir(exist_ok=True)
    for idx, (lc, rc) in enumerate(zip(left, right)):
        np.save(left_dir / f"{idx:04d}.npy", lc)
        np.save(right_dir / f"{idx:04d}.npy", rc)
        w, h = cam.image_size
        png = np.full((h, w, 3), 200, dtype=np.uint8)
        cv2.imwrite(str(frames_dir / f"{idx:04d}.png"), png)
    (sdir / "image_size.json").write_text(json.dumps({
        "image_size": list(cam.image_size), "stereo": True,
    }))
    # Bump session.json captures count (stereo expects >=3 pairs)
    session_json = sdir / "session.json"
    data = json.loads(session_json.read_text())
    data["captures"] = len(left)
    session_json.write_text(json.dumps(data, indent=2))
    from server.routes import sessions as sessions_mod
    sessions_mod._INDEX.clear()


# ---------------------------------------------------------------------------
# /calibrate/{id} — happy path (mono)
# ---------------------------------------------------------------------------

def test_calibrate_endpoint_runs_and_returns_200(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)

    resp = http_client.post(f"/calibrate/{sid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == sid
    assert isinstance(body["reprojection_error"], float)
    assert isinstance(body["rms"], float)
    assert body["image_size"] == list(synthetic_cam.image_size)


def test_calibrate_writes_all_three_artifact_files(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)

    resp = http_client.post(f"/calibrate/{sid}")
    body = resp.json()
    for path in (body["npz_path"], body["yaml_path"], body["meta_path"]):
        assert Path(path).exists(), f"missing artifact: {path}"


def test_calibrate_npz_round_trip(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)

    body = http_client.post(f"/calibrate/{sid}").json()
    data = np.load(body["npz_path"], allow_pickle=True)

    # Required keys
    for key in ("camera_matrix", "dist_coeffs", "image_size", "rms",
                "reprojection_error", "rvecs", "tvecs",
                "profile_inner_corners_x", "profile_inner_corners_y",
                "profile_square_size_mm"):
        assert key in data.files, f"missing key {key} in npz"

    # Recovered K should be within 5 % of ground truth (synthetic, low-noise)
    K = data["camera_matrix"]
    assert K[0, 0] == pytest.approx(synthetic_cam.K[0, 0], rel=0.05)
    assert K[1, 1] == pytest.approx(synthetic_cam.K[1, 1], rel=0.05)
    # Principal point within 5 % of image center
    assert abs(K[0, 2] - synthetic_cam.K[0, 2]) / synthetic_cam.image_size[0] < 0.05
    assert abs(K[1, 2] - synthetic_cam.K[1, 2]) / synthetic_cam.image_size[1] < 0.05

    # image_size preserved
    assert data["image_size"].tolist() == list(synthetic_cam.image_size)
    # Profile block preserved
    assert int(data["profile_inner_corners_x"]) == synthetic_cam.board_w
    assert int(data["profile_square_size_mm"]) == synthetic_cam.square_size_mm
    # rms positive
    assert float(data["rms"]) > 0


def test_calibrate_yaml_round_trip(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=12)

    body = http_client.post(f"/calibrate/{sid}").json()
    fs = cv2.FileStorage(body["yaml_path"], cv2.FILE_STORAGE_READ)
    try:
        for key in ("camera_matrix", "distortion_coefficients",
                    "image_width", "image_height", "rms",
                    "square_size_mm", "board_width", "board_height",
                    "rvec_0", "tvec_0"):
            node = fs.getNode(key)
            assert not node.empty(), f"missing {key} in result.yaml"
        assert int(fs.getNode("image_width").real()) == synthetic_cam.image_size[0]
        assert int(fs.getNode("image_height").real()) == synthetic_cam.image_size[1]
        assert int(fs.getNode("board_width").real()) == synthetic_cam.board_w
        assert fs.getNode("square_size_mm").real() == pytest.approx(synthetic_cam.square_size_mm)
        # rms in the synthetic-noise regime
        assert fs.getNode("rms").real() < 1.0
    finally:
        fs.release()


def test_calibrate_meta_json_round_trip(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)

    body = http_client.post(f"/calibrate/{sid}").json()
    meta = json.loads(Path(body["meta_path"]).read_text())
    assert meta["n_captures"] == 15
    assert meta["image_size"] == list(synthetic_cam.image_size)
    assert meta["profile"]["inner_corners_x"] == synthetic_cam.board_w
    assert meta["profile"]["square_size_mm"] == synthetic_cam.square_size_mm
    assert meta["rms"] > 0
    # saved_at is a recent ISO-8601 timestamp
    assert "T" in meta["saved_at"]


def test_calibrate_session_state_marks_finished(http_client, tmp_data_dirs, synthetic_cam):
    """The session should transition to FINISHED with rms populated."""
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)

    http_client.post(f"/calibrate/{sid}")
    info = http_client.get(f"/sessions/{sid}").json()
    assert info["state"] == "finished"
    assert info["rms"] is not None
    assert info["reprojection_error"] is not None
    assert len(info["result_files"]) == 3


def test_calibrate_too_few_captures_returns_400(http_client, tmp_data_dirs, synthetic_cam):
    """< 3 corner files must produce a 400 (the calibration core raises ValueError)."""
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    sdir.joinpath("corners").mkdir(exist_ok=True)
    sdir.joinpath("frames").mkdir(exist_ok=True)
    # Only one fake capture — too few
    corners = np.zeros((synthetic_cam.num_corners, 1, 2), dtype=np.float32)
    np.save(sdir / "corners" / "0000.npy", corners)
    w, h = synthetic_cam.image_size
    cv2.imwrite(str(sdir / "frames" / "0000.png"), np.full((h, w, 3), 200, dtype=np.uint8))
    (sdir / "image_size.json").write_text(json.dumps({"image_size": list(synthetic_cam.image_size)}))

    resp = http_client.post(f"/calibrate/{sid}")
    assert resp.status_code == 400
    assert "captures" in resp.json()["detail"].lower()


def test_calibrate_missing_capture_dir_returns_400(http_client):
    sid = _create_session(http_client)
    # No corners/ folder at all
    resp = http_client.post(f"/calibrate/{sid}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /calibrate/{id}/intrinsics
# ---------------------------------------------------------------------------

def test_intrinsics_endpoint_returns_parseable_yaml(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)
    http_client.post(f"/calibrate/{sid}")

    resp = http_client.get(f"/calibrate/{sid}/intrinsics")
    assert resp.status_code == 200
    body = resp.json()
    assert "yaml" in body
    assert body["name"]
    assert body["session_id"] == sid

    # YAML must parse with yaml.safe_load
    import yaml
    parsed = yaml.safe_load(body["yaml"])
    assert isinstance(parsed, dict)
    assert "camera_matrix" in parsed
    assert "dist_coeffs" in parsed
    assert "image_width" in parsed
    assert "image_height" in parsed
    # Values match recovered K
    K = parsed["camera_matrix"]
    assert pytest.approx(K["fx"], rel=0.05) == synthetic_cam.K[0, 0]
    assert pytest.approx(K["fy"], rel=0.05) == synthetic_cam.K[1, 1]
    # Header comments mention rms
    assert "RMS" in body["yaml"]


def test_intrinsics_endpoint_404_before_calibration(http_client):
    sid = _create_session(http_client)
    resp = http_client.get(f"/calibrate/{sid}/intrinsics")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /calibrate/{id}/files
# ---------------------------------------------------------------------------

def test_files_endpoint_lists_three_artifact_paths(http_client, tmp_data_dirs, synthetic_cam):
    sid = _create_session(http_client)
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_corners(sdir, synthetic_cam, n_captures=15)
    http_client.post(f"/calibrate/{sid}")

    resp = http_client.get(f"/calibrate/{sid}/files")
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert len(files) == 3
    assert any(f.endswith(".npz") for f in files)
    assert any(f.endswith(".yaml") for f in files)
    assert any(f.endswith("meta.json") for f in files)


# ---------------------------------------------------------------------------
# Stereo round-trip
# ---------------------------------------------------------------------------

def test_stereo_calibrate_round_trip(http_client, tmp_data_dirs):
    sid = _create_session(http_client, mode="stereo_separate", name="stereo_ci")
    sdir = tmp_data_dirs["sessions"] / sid
    _seed_stereo_corners(sdir, default_synthetic_camera(), baseline_mm=60.0, n_pairs=12)

    resp = http_client.post(f"/calibrate/{sid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    data = np.load(body["npz_path"], allow_pickle=True)
    assert bool(data["stereo"])
    assert "K1" in data.files and "K2" in data.files
    assert "R" in data.files and "T" in data.files
    assert "baseline_mm" in data.files
    assert float(data["baseline_mm"]) == pytest.approx(60.0, rel=0.05)

    # YAML should expose both eye blocks + R/T + baseline_mm
    fs = cv2.FileStorage(body["yaml_path"], cv2.FILE_STORAGE_READ)
    try:
        for key in ("stereo", "K1", "D1", "K2", "D2", "R", "T", "rms", "baseline_mm"):
            assert not fs.getNode(key).empty(), f"missing {key} in stereo yaml"
        assert fs.getNode("stereo").real() == 1.0
    finally:
        fs.release()

    # meta.json marks stereo
    meta = json.loads(Path(body["meta_path"]).read_text())
    assert meta["stereo"] is True
    assert meta["baseline_mm"] == pytest.approx(60.0, rel=0.05)


# ---------------------------------------------------------------------------
# Regression: legacy `stereo: true` session.json files still calibrate
# ---------------------------------------------------------------------------

def test_legacy_session_with_stereo_true_still_calibrates(http_client, tmp_data_dirs, synthetic_cam):
    """An on-disk session.json using the OLD `stereo: true` field (no `mode`)
    must still calibrate, because `_load_chessboard_for_session` migrates it
    to `mode: stereo_lr` (which routes into `calibrate_stereo`)."""
    sid = _create_session(http_client, name="legacy_calib")
    sdir = tmp_data_dirs["sessions"] / sid
    # Rewrite session.json with the old `stereo: true` shape (no `mode`)
    legacy = json.loads((sdir / "session.json").read_text())
    legacy["profile"].pop("mode", None)
    legacy["profile"]["stereo"] = True
    (sdir / "session.json").write_text(json.dumps(legacy, indent=2))

    # `stereo: true` → `mode=stereo_lr` → route looks for corners_left/ and
    # corners_right/. Use the stereo seed helper.
    _seed_stereo_corners(sdir, synthetic_cam, baseline_mm=60.0, n_pairs=12)
    resp = http_client.post(f"/calibrate/{sid}")
    assert resp.status_code == 200, resp.text