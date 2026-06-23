"""Helpers shared between the streaming layer and the sessions route module.

Kept separate to avoid circular imports (runtime.py <-> routes/sessions.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core.storage import session_dir
from ..models.schemas import Profile


def load_profile_for_session(session_id: str) -> Profile:
    rec_path = session_dir(session_id) / "session.json"
    if not rec_path.exists():
        # Sensible default if session.json is missing (shouldn't happen post-create).
        return Profile(name="default", inner_corners_x=9, inner_corners_y=6, square_size_mm=25.0)
    rec = json.loads(rec_path.read_text())
    return Profile(**rec["profile"])


def bump_capture(session_id: str) -> None:
    """Mirror the count into session.json so /sessions/{id} reflects progress."""
    from ..routes.sessions import bump_capture as _bump  # late import
    _bump(session_id)