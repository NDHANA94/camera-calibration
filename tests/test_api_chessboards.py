"""API integration tests for /chessboards (and legacy /profiles alias)."""
from __future__ import annotations

import pytest


def _payload(name: str = "test_cb", mode: str = "mono", **overrides) -> dict:
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


# ---------------------------------------------------------------------------
# /chessboards CRUD
# ---------------------------------------------------------------------------

def test_create_chessboard(http_client, tmp_data_dirs):
    resp = http_client.post("/chessboards", json=_payload())
    assert resp.status_code == 201
    assert (tmp_data_dirs["profiles"] / "test_cb.json").exists()


def test_create_chessboard_duplicate_returns_409(http_client):
    http_client.post("/chessboards", json=_payload())
    resp = http_client.post("/chessboards", json=_payload())
    assert resp.status_code == 409


def test_create_chessboard_invalid_name_returns_400(http_client):
    for bad in ["has space", "../escape", "slash/in/name"]:
        resp = http_client.post("/chessboards", json=_payload(name=bad))
        assert resp.status_code == 400, f"expected 400 for {bad!r}, got {resp.status_code}"


def test_create_chessboard_empty_name_returns_422(http_client):
    """Empty name fails Pydantic's `min_length=1` validator with a 422."""
    resp = http_client.post("/chessboards", json=_payload(name=""))
    assert resp.status_code == 422


def test_save_endpoint_overwrites_existing(http_client, tmp_data_dirs):
    """POST /chessboards/save creates or overwrites — used by the UI Save button."""
    http_client.post("/chessboards/save", json=_payload(name="save_test"))
    updated = _payload(name="save_test", inner_corners_x=10, square_size_mm=50.0)
    resp = http_client.post("/chessboards/save", json=updated)
    assert resp.status_code == 200
    assert resp.json()["inner_corners_x"] == 10
    # Verify on disk
    import json
    on_disk = json.loads((tmp_data_dirs["profiles"] / "save_test.json").read_text())
    assert on_disk["inner_corners_x"] == 10
    assert on_disk["square_size_mm"] == 50.0


def test_list_chessboards_returns_created(http_client):
    http_client.post("/chessboards", json=_payload(name="a"))
    http_client.post("/chessboards", json=_payload(name="b"))
    resp = http_client.get("/chessboards")
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    assert {"a", "b"}.issubset(names)


def test_get_chessboard_by_name(http_client):
    http_client.post("/chessboards", json=_payload(name="by_name"))
    resp = http_client.get("/chessboards/by_name")
    assert resp.status_code == 200
    assert resp.json()["name"] == "by_name"


def test_get_chessboard_unknown_returns_404(http_client):
    resp = http_client.get("/chessboards/nope")
    assert resp.status_code == 404


def test_patch_chessboard_updates_fields(http_client, tmp_data_dirs):
    http_client.post("/chessboards", json=_payload(name="patch_me"))
    resp = http_client.patch("/chessboards/patch_me", json={"square_size_mm": 50.0})
    assert resp.status_code == 200
    assert resp.json()["square_size_mm"] == 50.0
    # Other fields preserved
    assert resp.json()["inner_corners_x"] == 8


def test_delete_chessboard_removes_file(http_client, tmp_data_dirs):
    http_client.post("/chessboards", json=_payload(name="delete_me"))
    resp = http_client.delete("/chessboards/delete_me")
    assert resp.status_code == 204
    assert not (tmp_data_dirs["profiles"] / "delete_me.json").exists()


def test_delete_unknown_returns_404(http_client):
    resp = http_client.delete("/chessboards/nope")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Legacy /profiles alias — must serve the same data
# ---------------------------------------------------------------------------

def test_legacy_profiles_alias_lists_same_data(http_client):
    http_client.post("/chessboards", json=_payload(name="shared_data"))
    via_chessboards = http_client.get("/chessboards").json()
    via_profiles = http_client.get("/profiles").json()
    # Both must include the just-created entry
    cb_names = {c["name"] for c in via_chessboards}
    pr_names = {c["name"] for c in via_profiles}
    assert "shared_data" in cb_names
    assert "shared_data" in pr_names


def test_legacy_profiles_alias_creates(http_client, tmp_data_dirs):
    """POST /profiles should write to the same on-disk file."""
    resp = http_client.post("/profiles", json=_payload(name="via_alias"))
    assert resp.status_code == 201
    assert (tmp_data_dirs["profiles"] / "via_alias.json").exists()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_chessboard_with_stereo_separate_mode_accepted(http_client):
    payload = _payload(mode="stereo_separate", name="sep_mode")
    resp = http_client.post("/chessboards", json=payload)
    assert resp.status_code == 201
    assert resp.json()["mode"] == "stereo_separate"


def test_chessboard_with_rational_flag_preserved(http_client):
    """Calibration flag bits must round-trip through the JSON wire format."""
    payload = _payload(name="flagged", flags=16384 + 4096)
    resp = http_client.post("/chessboards", json=payload)
    assert resp.status_code == 201
    assert resp.json()["flags"] == 16384 + 4096