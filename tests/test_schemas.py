"""Unit tests for `server.models.schemas` (Chessboard, CameraMode, sessions)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.models.schemas import (
    CalibrationFlags,
    CameraKind,
    CameraMode,
    Chessboard,
    ChessboardUpdate,
    Profile,                     # legacy alias
    SessionCreate,
    SessionInfo,
    SessionSource,
    SessionState,
)


# ---------------------------------------------------------------------------
# Chessboard round-trip
# ---------------------------------------------------------------------------

def test_chessboard_round_trip():
    cb = Chessboard(
        name="8x5_30mm",
        inner_corners_x=8,
        inner_corners_y=5,
        square_size_mm=30.0,
        flags=int(CalibrationFlags.RATIONAL_MODEL),
        required_captures=15,
        mode=CameraMode.STEREO_LR,
    )
    dumped = cb.model_dump()
    restored = Chessboard(**dumped)
    assert restored == cb


def test_profile_alias_is_same_class():
    """Legacy `Profile` import path must still construct a Chessboard."""
    assert Profile is Chessboard


def test_chessboard_default_mode_is_mono():
    cb = Chessboard(
        name="x", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
    )
    assert cb.mode == CameraMode.MONO
    assert cb.is_stereo is False


def test_chessboard_is_stereo_true_for_both_stereo_modes():
    for mode in (CameraMode.STEREO_LR, CameraMode.STEREO_SEPARATE):
        cb = Chessboard(
            name="s", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0, mode=mode,
        )
        assert cb.is_stereo is True


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kw", [
    {"inner_corners_x": 1},
    {"inner_corners_x": 31},
    {"inner_corners_y": 1},
    {"square_size_mm": 0.0},
    {"square_size_mm": 600.0},
    {"required_captures": 2},
    {"required_captures": 201},
])
def test_chessboard_field_bounds(kw):
    base = dict(
        name="x", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
    )
    with pytest.raises(ValidationError):
        Chessboard(**{**base, **kw})


def test_chessboard_name_max_length():
    with pytest.raises(ValidationError):
        Chessboard(
            name="x" * 65, inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
        )


def test_chessboard_name_empty_rejected():
    with pytest.raises(ValidationError):
        Chessboard(
            name="", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
        )


# ---------------------------------------------------------------------------
# CameraMode enum
# ---------------------------------------------------------------------------

def test_camera_mode_values():
    assert CameraMode.MONO.value == "mono"
    assert CameraMode.STEREO_LR.value == "stereo_lr"
    assert CameraMode.STEREO_SEPARATE.value == "stereo_separate"


def test_camera_mode_serialization_round_trip():
    for mode in CameraMode:
        dumped = mode.value
        assert CameraMode(dumped) is mode


def test_camera_kind_values():
    assert CameraKind.V4L2.value == "v4l2"
    assert CameraKind.RTSP.value == "rtsp"
    assert CameraKind.ARAVIS.value == "aravis"


# ---------------------------------------------------------------------------
# CalibrationFlags bit-mask
# ---------------------------------------------------------------------------

def test_calibration_flags_bit_mask_values():
    """Bit values must not collide with each other."""
    flags = [
        CalibrationFlags.FIX_K1, CalibrationFlags.FIX_K2, CalibrationFlags.FIX_K3,
        CalibrationFlags.FIX_K4, CalibrationFlags.FIX_K5,
        CalibrationFlags.FIX_ASPECT_RATIO,
        CalibrationFlags.ZERO_TANGENT_DIST,
        CalibrationFlags.RATIONAL_MODEL,
        CalibrationFlags.THIN_PRISM_MODEL,
        CalibrationFlags.FIX_S1_S2_S3_S4,
    ]
    bits = [int(f) for f in flags]
    assert len(set(bits)) == len(bits), "duplicate bit values"
    # Spot-check known values
    assert int(CalibrationFlags.FIX_ASPECT_RATIO) == 1024
    assert int(CalibrationFlags.ZERO_TANGENT_DIST) == 4096
    assert int(CalibrationFlags.RATIONAL_MODEL) == 16384


def test_calibration_flags_combinable():
    combo = (
        int(CalibrationFlags.FIX_ASPECT_RATIO)
        | int(CalibrationFlags.ZERO_TANGENT_DIST)
        | int(CalibrationFlags.RATIONAL_MODEL)
    )
    # Round-trip through Chessboard.flags
    cb = Chessboard(
        name="x", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
        flags=combo,
    )
    assert cb.flags == combo


# ---------------------------------------------------------------------------
# SessionCreate + SessionInfo
# ---------------------------------------------------------------------------

def test_session_create_accepts_local_without_camera_id():
    """Schema accepts the payload; the route layer (POST /sessions) is what
    enforces "camera_id required for local" with an HTTP 400."""
    sci = SessionCreate(name="s", source=SessionSource.LOCAL, chessboard=_cb())
    assert sci.camera_id is None
    assert sci.source == SessionSource.LOCAL


def test_session_create_does_not_require_camera_id_for_remote():
    """Remote cameras are discovered by the agent — `camera_id` not required
    in the create payload (it's filled in later via /ws/agent)."""
    sci = SessionCreate(name="r", source=SessionSource.REMOTE, chessboard=_cb())
    assert sci.camera_id is None


def test_session_info_includes_profile_field():
    """SessionInfo must expose the on-disk `profile` field (legacy name)."""
    si = SessionInfo(
        id="abcd1234", name="n", source=SessionSource.LOCAL,
        state=SessionState.RUNNING, captures=3, required_captures=15,
        profile=_cb(),
    )
    dumped = si.model_dump()
    assert "profile" in dumped
    assert dumped["profile"]["name"] == "test_cb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cb():
    return Chessboard(
        name="test_cb", inner_corners_x=8, inner_corners_y=5, square_size_mm=25.0,
    )