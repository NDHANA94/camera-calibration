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


class Profile(BaseModel):
    """Minimal calibration parameter set. Saved per-user."""

    name: str = Field(..., min_length=1, max_length=64)
    inner_corners_x: int = Field(..., ge=2, le=30)
    inner_corners_y: int = Field(..., ge=2, le=30)
    square_size_mm: float = Field(..., gt=0, le=500)
    flags: int = Field(default=0, ge=0)
    required_captures: int = Field(default=15, ge=3, le=200)


class ProfileCreate(Profile):
    pass


class ProfileUpdate(BaseModel):
    inner_corners_x: Optional[int] = Field(None, ge=2, le=30)
    inner_corners_y: Optional[int] = Field(None, ge=2, le=30)
    square_size_mm: Optional[float] = Field(None, gt=0, le=500)
    flags: Optional[int] = Field(None, ge=0)
    required_captures: Optional[int] = Field(None, ge=3, le=200)


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
    camera_id: Optional[str] = None      # required for LOCAL
    remote_token: Optional[str] = None   # for REMOTE: a pre-issued token (see routes/stream)
    profile: Profile                      # resolved at creation time


class SessionInfo(BaseModel):
    id: str
    name: str
    source: SessionSource
    camera_id: Optional[str] = None
    state: SessionState
    captures: int = 0
    required_captures: int = 15
    reprojection_error: Optional[float] = None
    rms: Optional[float] = None
    profile: Optional[Profile] = None
    result_files: List[str] = Field(default_factory=list)


class CalibrationResult(BaseModel):
    session_id: str
    reprojection_error: float
    rms: float
    image_size: List[int]
    profile: Profile
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