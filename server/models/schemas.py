"""Pydantic models used by the FastAPI server."""
from __future__ import annotations

from enum import Enum, IntEnum
from typing import List, Optional

from pydantic import BaseModel, Field


class CameraKind(str, Enum):
    """Logical camera source kinds supported by the server."""

    V4L2 = "v4l2"          # /dev/videoX, built-in webcam, USB, MIPI/CSI (via libcamera)
    RTSP = "rtsp"          # IP / RTSP network cameras
    ARAVIS = "aravis"      # Industrial GigE / GenICam (requires aravis bindings)


class CameraMode(str, Enum):
    """How the camera source maps to one or two capture streams.

    MONO             - one camera, one frame, mono calibration.
    STEREO_LR        - one camera, frame is split left|right, stereo calibration.
    STEREO_SEPARATE  - two cameras (left + right), each provides one frame,
                       paired at capture time for stereo calibration.
    """

    MONO = "mono"
    STEREO_LR = "stereo_lr"
    STEREO_SEPARATE = "stereo_separate"


class CameraInfo(BaseModel):
    """Description of a camera discovered on the local machine."""

    id: str = Field(..., description="Stable identifier (e.g. /dev/video0 or rtsp://...)")
    kind: CameraKind
    label: str = Field(..., description="Human-readable label for the UI")
    resolution: Optional[List[int]] = Field(None, description="[width, height] if known")


class CalibrationFlags(IntEnum):
    """Bit flags accepted by cv2.calibrateCamera."""

    NONE = 0
    FIX_K1 = 2
    FIX_K2 = 4
    FIX_K3 = 8
    FIX_K4 = 16
    FIX_K5 = 32
    FIX_ASPECT_RATIO = 1024
    ZERO_TANGENT_DIST = 4096
    RATIONAL_MODEL = 16384
    THIN_PRISM_MODEL = 32768
    FIX_S1_S2_S3_S4 = 65536


class Chessboard(BaseModel):
    """Saved chessboard + capture parameter set. User-named.

    `mode` was added together with the dual-camera feature; older profiles
    stored on disk may not have it -- the API defaults it to MONO.
    """

    name: str = Field(..., min_length=1, max_length=64)
    inner_corners_x: int = Field(..., ge=2, le=30)
    inner_corners_y: int = Field(..., ge=2, le=30)
    square_size_mm: float = Field(..., gt=0, le=500)
    flags: int = Field(default=0, ge=0)
    required_captures: int = Field(default=15, ge=3, le=200)
    # New in v2: which camera-mode this chessboard is meant for.
    mode: CameraMode = Field(
        default=CameraMode.MONO,
        description="Camera-mode this chessboard is meant for.",
    )

    # Legacy alias retained for backward-compat with old session.json files.
    stereo: bool = Field(
        default=False,
        description="DEPRECATED: kept for backward-compat. Use `mode` instead. "
        "True means STEREO_LR.",
    )

    @property
    def is_stereo(self) -> bool:
        """True for any stereo mode (LR or separate)."""
        return self.mode in (CameraMode.STEREO_LR, CameraMode.STEREO_SEPARATE)


# Backward-compat alias: existing code paths and on-disk JSON files reference
# `Profile`.  Keep the name available so nothing breaks.
Profile = Chessboard
ProfileCreate = Chessboard


class ChessboardUpdate(BaseModel):
    inner_corners_x: Optional[int] = Field(None, ge=2, le=30)
    inner_corners_y: Optional[int] = Field(None, ge=2, le=30)
    square_size_mm: Optional[float] = Field(None, gt=0, le=500)
    flags: Optional[int] = Field(None, ge=0)
    required_captures: Optional[int] = Field(None, ge=3, le=200)
    mode: Optional[CameraMode] = Field(None)


ProfileUpdate = ChessboardUpdate


class SessionSource(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class SessionState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


class SessionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    source: SessionSource
    camera_id: Optional[str] = None        # primary camera id (always required)
    camera_id_2: Optional[str] = None      # second camera id (STEREO_SEPARATE only)
    remote_token: Optional[str] = None     # for REMOTE: a pre-issued token (see routes/stream)
    chessboard: Chessboard                   # resolved at creation time (alias: profile)


class SessionInfo(BaseModel):
    id: str
    name: str
    source: SessionSource
    camera_id: Optional[str] = None
    camera_id_2: Optional[str] = None
    state: SessionState
    captures: int = 0
    required_captures: int = 15
    reprojection_error: Optional[float] = None
    rms: Optional[float] = None
    # Keep `profile` as the canonical field name on-disk; the UI may refer to
    # it as "chessboard" but renaming the field would invalidate existing
    # session.json files.
    profile: Optional[Chessboard] = None
    result_files: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None       # ISO-8601 UTC


class CalibrationResult(BaseModel):
    session_id: str
    reprojection_error: float
    rms: float
    image_size: List[int]
    chessboard: Chessboard
    npz_path: str
    yaml_path: str
    meta_path: str


class RemoteTokenRequest(BaseModel):
    session_id: str


class RemoteTokenResponse(BaseModel):
    token: str
    server_url: str
    session_id: str
    agent_command: str