"""Filesystem paths used by the server. Kept in one place so other modules
don't sprinkle join() calls around the codebase."""
from __future__ import annotations

from pathlib import Path

# server/core/storage.py -> server/core -> server -> repo root
_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _ROOT / "server" / "data"
PROFILES_DIR = DATA_DIR / "profiles"
SESSIONS_DIR = DATA_DIR / "sessions"
STATIC_DIR = _ROOT / "server" / "static"


def ensure_dirs() -> None:
    """Create data dirs on startup so the first write doesn't race."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def profile_path(name: str) -> Path:
    return PROFILES_DIR / f"{name}.json"


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def session_frames_dir(session_id: str) -> Path:
    return session_dir(session_id) / "frames"