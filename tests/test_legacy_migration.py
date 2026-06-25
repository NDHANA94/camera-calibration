"""Unit tests for legacy-chessboard migration logic."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.core.sessions_helpers import load_chessboard_for_session
from server.models.schemas import CameraMode, Chessboard
from server.routes.profiles import _load_chessboard_from_disk
from server.routes.sessions import _migrate_legacy_chessboard


# ---------------------------------------------------------------------------
# _migrate_legacy_chessboard (in sessions.py)
# ---------------------------------------------------------------------------

def test_migrate_legacy_stereo_true_to_stereo_lr():
    cb = {
        "name": "old",
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "stereo": True,
    }
    migrated = _migrate_legacy_chessboard(cb)
    assert migrated["mode"] == CameraMode.STEREO_LR.value


def test_migrate_legacy_no_stereo_to_mono():
    cb = {
        "name": "old",
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
    }
    migrated = _migrate_legacy_chessboard(cb)
    assert migrated["mode"] == CameraMode.MONO.value


def test_migrate_does_not_overwrite_existing_mode():
    cb = {
        "name": "new",
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "mode": "stereo_separate",
    }
    migrated = _migrate_legacy_chessboard(cb)
    assert migrated["mode"] == "stereo_separate"  # preserved


def test_migrate_is_idempotent():
    cb = {
        "name": "x", "inner_corners_x": 8, "inner_corners_y": 5,
        "square_size_mm": 30.0, "stereo": True,
    }
    once = _migrate_legacy_chessboard(dict(cb))
    twice = _migrate_legacy_chessboard(once)
    assert once == twice


# ---------------------------------------------------------------------------
# load_chessboard_for_session (in core/sessions_helpers.py)
# ---------------------------------------------------------------------------

def test_load_chessboard_from_legacy_session_json(tmp_path: Path):
    """Hand-written session.json (no `mode`, only `stereo: true`) should load
    as a Chessboard with `mode=stereo_lr`."""
    session_id = "abcdef1234"
    sdir = tmp_path / session_id
    sdir.mkdir()
    (sdir / "session.json").write_text(json.dumps({
        "id": session_id,
        "name": "legacy_session",
        "source": "local",
        "camera_id": "/dev/video0",
        "state": "finished",
        "captures": 5,
        "required_captures": 5,
        "profile": {
            "name": "8x5_30mm",
            "inner_corners_x": 8,
            "inner_corners_y": 5,
            "square_size_mm": 30.0,
            "flags": 0,
            "stereo": True,
        },
    }))
    # Patch SESSIONS_DIR so the helper looks in tmp_path
    from server.core import storage as storage_mod
    storage_mod.SESSIONS_DIR = tmp_path

    cb = load_chessboard_for_session(session_id)
    assert isinstance(cb, Chessboard)
    assert cb.mode == CameraMode.STEREO_LR
    assert cb.inner_corners_x == 8
    assert cb.square_size_mm == 30.0


def test_load_chessboard_from_modern_session_json(tmp_path: Path):
    """Modern session.json (with `mode`) loads as-is."""
    session_id = "modern123"
    sdir = tmp_path / session_id
    sdir.mkdir()
    (sdir / "session.json").write_text(json.dumps({
        "id": session_id,
        "name": "modern",
        "source": "local",
        "camera_id": "/dev/video0",
        "state": "running",
        "captures": 1,
        "required_captures": 10,
        "profile": {
            "name": "modern_cb",
            "inner_corners_x": 9,
            "inner_corners_y": 6,
            "square_size_mm": 25.0,
            "flags": 0,
            "mode": "mono",
        },
    }))
    from server.core import storage as storage_mod
    storage_mod.SESSIONS_DIR = tmp_path

    cb = load_chessboard_for_session(session_id)
    assert cb.mode == CameraMode.MONO
    assert cb.inner_corners_x == 9


def test_load_chessboard_missing_session_returns_default(tmp_path: Path):
    """If session.json doesn't exist, return a sensible default rather than crashing."""
    from server.core import storage as storage_mod
    storage_mod.SESSIONS_DIR = tmp_path
    cb = load_chessboard_for_session("does_not_exist")
    assert cb.mode == CameraMode.MONO
    assert cb.inner_corners_x >= 2


# ---------------------------------------------------------------------------
# _load_chessboard_from_disk (in routes/profiles.py)
# ---------------------------------------------------------------------------

def test_load_chessboard_from_legacy_disk_file(tmp_path: Path, monkeypatch):
    """A profile JSON with `stereo: true` but no `mode` loads as stereo_lr."""
    from server.core import storage as storage_mod
    monkeypatch.setattr(storage_mod, "PROFILES_DIR", tmp_path)

    (tmp_path / "legacy.json").write_text(json.dumps({
        "name": "legacy",
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "flags": 0,
        "stereo": True,
    }))
    cb = _load_chessboard_from_disk(tmp_path / "legacy.json")
    assert cb.mode == CameraMode.STEREO_LR


def test_load_chessboard_modern_disk_file(tmp_path: Path, monkeypatch):
    from server.core import storage as storage_mod
    monkeypatch.setattr(storage_mod, "PROFILES_DIR", tmp_path)

    (tmp_path / "modern.json").write_text(json.dumps({
        "name": "modern",
        "inner_corners_x": 8,
        "inner_corners_y": 5,
        "square_size_mm": 30.0,
        "flags": 0,
        "mode": "stereo_separate",
    }))
    cb = _load_chessboard_from_disk(tmp_path / "modern.json")
    assert cb.mode == CameraMode.STEREO_SEPARATE