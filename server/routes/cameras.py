"""GET /cameras and /cameras/{id}/probe."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.camera_manager import list_cameras, CameraCapture
from ..models.schemas import CameraInfo

router = APIRouter(prefix="/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraInfo])
def get_cameras() -> list[CameraInfo]:
    return list_cameras()


@router.get("/{camera_id:path}/probe", response_model=CameraInfo)
def probe_camera(camera_id: str) -> CameraInfo:
    """Open the camera briefly and return its actual negotiated resolution."""
    try:
        with CameraCapture(camera_id) as cap:
            frame = cap.read()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if frame is None:
        raise HTTPException(status_code=400, detail="Camera produced no frames")
    return CameraInfo(
        id=camera_id,
        kind="v4l2",  # best-effort; the UI displays only the label
        label=camera_id,
        resolution=[int(frame.shape[1]), int(frame.shape[0])],
    )