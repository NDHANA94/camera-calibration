"""Helpers shared between the streaming layer and the sessions route module.

Kept separate to avoid circular imports (runtime.py <-> routes/sessions.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core.storage import session_dir
from ..models.schemas import Chessboard, CameraMode


def load_chessboard_for_session(session_id: str) -> Chessboard:
    """Read the chessboard (stored under session.json's ``profile`` key) and
    normalise legacy fields (``stereo: true`` -> ``mode: stereo_lr``)."""
    rec_path = session_dir(session_id) / "session.json"
    if not rec_path.exists():
        # Sensible default if session.json is missing (shouldn't happen post-create).
        return Chessboard(
            name="default", inner_corners_x=9, inner_corners_y=6, square_size_mm=25.0,
        )
    rec = json.loads(rec_path.read_text())
    cb = dict(rec.get("profile") or {})
    # Legacy: ``stereo: true`` -> ``mode: stereo_lr``
    if "mode" not in cb and cb.get("stereo"):
        cb["mode"] = CameraMode.STEREO_LR.value
    if "mode" not in cb:
        cb["mode"] = CameraMode.MONO.value
    return Chessboard(**cb)


# Backward-compat alias for older callers.
def load_profile_for_session(session_id: str) -> Chessboard:
    return load_chessboard_for_session(session_id)


def bump_capture(session_id: str) -> None:
    """Mirror the count into session.json so /sessions/{id} reflects progress."""
    from ..routes.sessions import bump_capture as _bump  # late import
    _bump(session_id)
