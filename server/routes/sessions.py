"""Session lifecycle: create, list, fetch, finish, delete.

Sessions are *not* stateful server objects — they live as directories on disk
under server/data/sessions/<id>/. The streaming layer (routes/stream.py)
writes captured frames into that directory and the calibration route reads
them back when finishing.
"""
from __future__ import annotations

import json
import platform
import secrets
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from fastapi import APIRouter, HTTPException

from ..core.storage import session_dir, session_frames_dir
from ..models.schemas import (
    Profile,
    SessionCreate,
    SessionInfo,
    SessionSource,
    SessionState,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])

# In-memory index of sessions for fast lookup. Source of truth is the filesystem.
_INDEX: Dict[str, dict] = {}


def _new_session_id() -> str:
    return secrets.token_hex(6)


def _record_path(session_id: str) -> Path:
    return session_dir(session_id) / "session.json"


def _load_index_from_disk() -> None:
    if _INDEX:
        return
    base = session_dir("__missing__").parent
    if not base.exists():
        return
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        rec = _record_path(d.name)
        if rec.exists():
            try:
                _INDEX[d.name] = json.loads(rec.read_text())
            except Exception:
                continue


def _persist(session_id: str) -> None:
    rec = _INDEX[session_id]
    path = _record_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, indent=2))


@router.get("", response_model=List[SessionInfo])
def list_sessions() -> List[SessionInfo]:
    _load_index_from_disk()
    return [SessionInfo(**v) for v in _INDEX.values()]


@router.post("", response_model=SessionInfo, status_code=201)
def create_session(payload: SessionCreate) -> SessionInfo:
    if payload.source == SessionSource.LOCAL and not payload.camera_id:
        raise HTTPException(400, "camera_id required for local sessions")
    sid = _new_session_id()
    session_dir(sid).mkdir(parents=True, exist_ok=True)
    session_frames_dir(sid).mkdir(parents=True, exist_ok=True)
    rec = {
        "id": sid,
        "name": payload.name,
        "source": payload.source,
        "camera_id": payload.camera_id,
        "state": SessionState.IDLE,
        "captures": 0,
        "required_captures": payload.profile.required_captures,
        "reprojection_error": None,
        "result_files": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": payload.profile.model_dump(),
    }
    _INDEX[sid] = rec
    _persist(sid)
    return SessionInfo(**rec)


def get_session(session_id: str) -> SessionInfo:
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    return SessionInfo(**rec)


# ---------- Session health (must be registered before /{session_id}) ----------

def _compute_health(session_id: str, rec: dict) -> dict:
    """Derive a health indicator for one session from available on-disk data."""
    state = rec.get("state", "idle")
    captures = int(rec.get("captures", 0))
    required = int(rec.get("required_captures", 1))

    # Finished: primary metric is RMS reprojection error
    if state == "finished":
        rms = rec.get("rms")
        if rms is None:
            # Legacy session: read from npz
            try:
                data = np.load(str(session_dir(session_id) / "result.npz"), allow_pickle=True)
                rms = float(data["rms"])
            except Exception:
                rms = None
        if rms is not None:
            if rms < 0.3:
                return {"label": f"RMS {rms:.3f}", "color": "ok",   "tip": "Excellent — very tight reprojection"}
            if rms < 0.7:
                return {"label": f"RMS {rms:.3f}", "color": "ok",   "tip": "Good calibration"}
            if rms < 1.5:
                return {"label": f"RMS {rms:.3f}", "color": "warn", "tip": "Acceptable — consider recollecting"}
            return     {"label": f"RMS {rms:.3f}", "color": "bad",  "tip": "Poor — high reprojection error"}
        return {"label": "no metrics", "color": "muted", "tip": "Result files missing"}

    # Sessions with at least 2 captures: measure pose-coverage diversity
    if captures >= 2:
        try:
            from ..core.guidance import _angle, _pose_axis
            corners_dir = session_dir(session_id) / "corners"
            files = sorted(corners_dir.glob("*.npy"))[:captures]
            if len(files) >= 2:
                loaded = [np.load(str(f)) for f in files]
                axes = [_pose_axis(c) for c in loaded]
                min_ang = min(
                    _angle(axes[i], axes[j])
                    for i in range(len(axes))
                    for j in range(i + 1, len(axes))
                )
                pct = int(100 * captures / max(required, 1))
                if min_ang < 8:
                    return {"label": f"{pct}% · low diversity",  "color": "bad",  "tip": f"Min pose angle {min_ang:.0f}° — tilt board more"}
                if min_ang < 20:
                    return {"label": f"{pct}% · fair diversity", "color": "warn", "tip": f"Min pose angle {min_ang:.0f}° — vary angles more"}
                return     {"label": f"{pct}% · good diversity", "color": "ok",   "tip": f"Min pose angle {min_ang:.0f}° — good coverage so far"}
        except Exception:
            pass
        return {"label": f"{captures}/{required} captures", "color": "muted", "tip": "Coverage not yet computable"}

    if captures == 1:
        return {"label": "1 capture", "color": "muted", "tip": "Need more captures"}
    return {"label": "no captures", "color": "muted", "tip": "Session not started"}


@router.get("/health")
def sessions_health() -> dict:
    """Return a health indicator for every known session (keyed by session ID)."""
    _load_index_from_disk()
    return {sid: _compute_health(sid, rec) for sid, rec in list(_INDEX.items())}


@router.get("/{session_id}", response_model=SessionInfo)
def get_session_route(session_id: str) -> SessionInfo:
    return get_session(session_id)


@router.get("/{session_id}/detail")
def get_session_detail(session_id: str) -> dict:
    """Full session detail: profile params + sorted list of captured frame filenames."""
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    sdir = session_dir(session_id)
    frames_dir = sdir / "frames"
    frames = sorted(f.name for f in frames_dir.glob("*.png")) if frames_dir.exists() else []
    return {**rec, "frames": frames}


@router.post("/{session_id}/start", response_model=SessionInfo)
def start_session(session_id: str) -> SessionInfo:
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    rec["state"] = SessionState.RUNNING
    rec["captures"] = 0
    _persist(session_id)
    return SessionInfo(**rec)


async def abort_session_runtime(session_id: str) -> None:
    """Flip the runtime's aborted flag and close any active stream.

    Called by the stream loop on the next iteration so it can clean up
    cleanly. Also sets the persisted state to IDLE so the UI can recover.
    """
    try:
        from ..core.runtime import get_runtime
        runtime = await get_runtime(session_id)
        runtime.aborted = True
    except Exception:
        pass
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is not None and rec.get("state") == SessionState.RUNNING:
        rec["state"] = SessionState.IDLE
        _persist(session_id)


@router.post("/{session_id}/abort", response_model=SessionInfo)
async def abort_session(session_id: str) -> SessionInfo:
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    await abort_session_runtime(session_id)
    return SessionInfo(**rec)


def bump_capture(session_id: str, *, error: Optional[float] = None) -> None:
    """Called by the streaming layer when a frame is captured."""
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        return
    rec["captures"] = int(rec.get("captures", 0)) + 1
    if error is not None:
        rec["reprojection_error"] = float(error)
    _persist(session_id)


def finish_session(
    session_id: str,
    *,
    error: Optional[float] = None,
    rms: Optional[float] = None,
    result_files: Optional[List[str]] = None,
    state: str = SessionState.FINISHED,
) -> None:
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        return
    rec["state"] = state
    if error is not None:
        rec["reprojection_error"] = float(error)
    if rms is not None:
        rec["rms"] = float(rms)
    if result_files:
        rec["result_files"] = list(result_files)
    _persist(session_id)


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str) -> None:
    _load_index_from_disk()
    rec = _INDEX.get(session_id)
    if rec is None:
        raise HTTPException(404, "Session not found")
    d = session_dir(session_id)
    if d.exists():
        shutil.rmtree(d)
    _INDEX.pop(session_id, None)


@router.post("/{session_id}/open-dir", status_code=200)
def open_session_directory(session_id: str) -> dict:
    """Open the session directory in the host file manager (xdg-open / open)."""
    sdir = session_dir(session_id)
    if not sdir.exists():
        raise HTTPException(404, "Session directory not found on disk")
    try:
        cmd = "open" if platform.system() == "Darwin" else "xdg-open"
        subprocess.Popen([cmd, str(sdir)])
        return {"path": str(sdir)}
    except Exception as exc:
        raise HTTPException(500, f"Could not open file manager: {exc}")