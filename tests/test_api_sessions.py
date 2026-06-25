"""API integration tests for /sessions (CRUD + health)."""
from __future__ import annotations

from pathlib import Path

import pytest


def _chessboard_payload(name: str = "test_cb", mode: str = "mono", **overrides) -> dict:
    base = {
        "name": name,
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "flags": 0,
        "required_captures": 5,
        "mode": mode,
    }
    base.update(overrides)
    return base


def _create_payload(name: str = "my_session", mode: str = "mono",
                    camera_id: str | None = "/dev/video0",
                    camera_id_2: str | None = None) -> dict:
    return {
        "name": name,
        "source": "local",
        "camera_id": camera_id,
        "camera_id_2": camera_id_2,
        "chessboard": _chessboard_payload(mode=mode),
    }


# ---------------------------------------------------------------------------
# POST /sessions
# ---------------------------------------------------------------------------

def test_create_session_returns_201_and_persists(http_client, tmp_data_dirs):
    resp = http_client.post("/sessions", json=_create_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "id" in data and len(data["id"]) >= 8
    assert data["state"] == "idle"
    assert data["captures"] == 0
    # Directory + session.json created
    session_dir = tmp_data_dirs["sessions"] / data["id"]
    assert session_dir.exists()
    assert (session_dir / "session.json").exists()


def test_create_session_uses_legacy_profile_key(http_client, tmp_data_dirs):
    """The on-disk schema uses `profile` (not `chessboard`) so legacy session.json
    files remain readable."""
    resp = http_client.post("/sessions", json=_create_payload())
    sid = resp.json()["id"]
    import json
    raw = json.loads((tmp_data_dirs["sessions"] / sid / "session.json").read_text())
    assert "profile" in raw
    assert raw["profile"]["name"] == "test_cb"
    assert raw["profile"]["mode"] == "mono"


def test_create_session_local_without_camera_id_returns_400(http_client):
    resp = http_client.post("/sessions", json=_create_payload(camera_id=None))
    assert resp.status_code == 400
    assert "camera_id" in resp.json()["detail"].lower()


def test_create_session_stereo_separate_requires_camera_id_2(http_client):
    payload = _create_payload(mode="stereo_separate", camera_id_2=None)
    resp = http_client.post("/sessions", json=payload)
    assert resp.status_code == 400
    assert "camera_id_2" in resp.json()["detail"]


def test_create_session_stereo_separate_rejects_identical_cameras(http_client):
    payload = _create_payload(
        mode="stereo_separate",
        camera_id="/dev/video0",
        camera_id_2="/dev/video0",
    )
    resp = http_client.post("/sessions", json=payload)
    assert resp.status_code == 400
    assert "must differ" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# GET /sessions and /sessions/{id}
# ---------------------------------------------------------------------------

def test_list_sessions_returns_created(http_client):
    ids = []
    for i in range(3):
        r = http_client.post("/sessions", json=_create_payload(name=f"s{i}"))
        ids.append(r.json()["id"])
    resp = http_client.get("/sessions")
    assert resp.status_code == 200
    listed = {s["id"] for s in resp.json()}
    assert set(ids).issubset(listed)


def test_get_session_returns_one(http_client):
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    resp = http_client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == sid


def test_get_session_unknown_returns_404(http_client):
    resp = http_client.get("/sessions/does_not_exist")
    assert resp.status_code == 404


def test_session_detail_includes_frames_list(http_client, tmp_data_dirs):
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    # Drop a fake frame in to confirm it's surfaced
    (tmp_data_dirs["sessions"] / sid / "frames").mkdir(exist_ok=True)
    (tmp_data_dirs["sessions"] / sid / "frames" / "0000.png").write_bytes(b"")
    (tmp_data_dirs["sessions"] / sid / "frames" / "0001.png").write_bytes(b"")
    resp = http_client.get(f"/sessions/{sid}/detail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["frames"] == ["0000.png", "0001.png"]


# ---------------------------------------------------------------------------
# DELETE /sessions/{id}
# ---------------------------------------------------------------------------

def test_delete_session_removes_directory(http_client, tmp_data_dirs):
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    resp = http_client.delete(f"/sessions/{sid}")
    assert resp.status_code == 204
    assert not (tmp_data_dirs["sessions"] / sid).exists()


def test_delete_unknown_returns_404(http_client):
    resp = http_client.delete("/sessions/does_not_exist")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /sessions/health
# ---------------------------------------------------------------------------

def test_health_idle_session_is_muted(http_client):
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    resp = http_client.get("/sessions/health")
    assert resp.status_code == 200
    body = resp.json()
    assert sid in body
    entry = body[sid]
    assert entry["color"] == "muted"
    assert "tip" in entry


def test_health_finished_session_with_rms_reports_color(http_client, tmp_data_dirs):
    """A finished session with rms < 0.7 should be reported as 'ok'."""
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    # Manually flip the session to finished + add an rms
    from server.routes.sessions import finish_session
    finish_session(sid, error=0.45, rms=0.45, result_files=["x"])
    resp = http_client.get("/sessions/health")
    entry = resp.json()[sid]
    assert entry["color"] == "ok"
    assert "0.45" in entry["label"]


def test_health_finished_session_high_rms_is_bad(http_client):
    sid = http_client.post("/sessions", json=_create_payload()).json()["id"]
    from server.routes.sessions import finish_session
    finish_session(sid, error=2.0, rms=2.0, result_files=["x"])
    resp = http_client.get("/sessions/health")
    entry = resp.json()[sid]
    assert entry["color"] == "bad"


# ---------------------------------------------------------------------------
# Persistence across requests (index is rebuilt from disk if cleared)
# ---------------------------------------------------------------------------

def test_session_persists_across_requests(http_client, tmp_data_dirs):
    sid = http_client.post("/sessions", json=_create_payload(name="persist_test")).json()["id"]
    # The TestClient wraps the same in-process app, so we don't simulate a
    # full restart here. Instead, clear the in-memory index and confirm the
    # session reloads from session.json on the next request.
    from server.routes import sessions as sessions_mod
    sessions_mod._INDEX.clear()
    resp = http_client.get(f"/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "persist_test"