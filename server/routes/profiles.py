"""CRUD for saved chessboard (calibration parameter) sets stored under
``server/data/profiles/``.

Conceptually a "chessboard" stores:
  - the physical board (inner corners W×H, square size)
  - calibration flags
  - which camera-mode the board is meant for (mono / stereo L|R / stereo separate)

Endpoints are exposed under BOTH ``/profiles`` (legacy alias) and
``/chessboards`` (new canonical name).  Both serve the same JSON files on disk.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from ..core.storage import profile_path
from ..models.schemas import Chessboard, ChessboardUpdate

# Both prefixes are exposed so old + new clients work.  ``/profiles`` is the
# legacy alias; ``/chessboards`` is the canonical new name.
router_profiles = APIRouter(prefix="/profiles", tags=["profiles"])
router_chessboards = APIRouter(prefix="/chessboards", tags=["chessboards"])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _load_chessboard_from_disk(path: Path) -> Chessboard:
    data = json.loads(path.read_text())
    # Legacy files may have `stereo: true` instead of `mode: stereo_lr`.  Map
    # that into the new enum so older profiles keep working.
    if "mode" not in data and data.get("stereo"):
        data["mode"] = "stereo_lr"
    if "mode" not in data:
        data["mode"] = "mono"
    return Chessboard(**data)


def _list() -> List[Chessboard]:
    out: List[Chessboard] = []
    base = profile_path("__nope__").parent
    for p in sorted(base.glob("*.json")):
        try:
            out.append(_load_chessboard_from_disk(p))
        except Exception:
            continue
    return out


def _save(payload: Chessboard) -> Chessboard:
    if not _SAFE_NAME.match(payload.name):
        raise HTTPException(
            400,
            f"Invalid chessboard name '{payload.name}'. "
            "Use only letters, digits, underscores, hyphens, or dots (no spaces).",
        )
    path = profile_path(payload.name)
    path.write_text(payload.model_dump_json(indent=2))
    return payload


# --- list --------------------------------------------------------------------

@router_profiles.get("", response_model=List[Chessboard])
@router_chessboards.get("", response_model=List[Chessboard])
def list_chessboards() -> List[Chessboard]:
    return _list()


# --- create ------------------------------------------------------------------

@router_profiles.post("", response_model=Chessboard, status_code=201)
@router_chessboards.post("", response_model=Chessboard, status_code=201)
def create_chessboard(payload: Chessboard) -> Chessboard:
    if not _SAFE_NAME.match(payload.name):
        raise HTTPException(400, "Invalid chessboard name")
    path = profile_path(payload.name)
    if path.exists():
        raise HTTPException(409, "Chessboard already exists")
    return _save(payload)


# --- create-or-overwrite -----------------------------------------------------

@router_profiles.post("/save", response_model=Chessboard)
@router_chessboards.post("/save", response_model=Chessboard)
def save_chessboard(payload: Chessboard) -> Chessboard:
    """Create-or-overwrite a chessboard. Used by the UI's Save button."""
    return _save(payload)


# --- get ---------------------------------------------------------------------

@router_profiles.get("/{name}", response_model=Chessboard)
@router_chessboards.get("/{name}", response_model=Chessboard)
def get_chessboard(name: str) -> Chessboard:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Chessboard not found")
    return _load_chessboard_from_disk(path)


# --- update (PATCH) ----------------------------------------------------------

@router_profiles.put("/{name}", response_model=Chessboard)
@router_chessboards.put("/{name}", response_model=Chessboard)
@router_profiles.patch("/{name}", response_model=Chessboard)
@router_chessboards.patch("/{name}", response_model=Chessboard)
def update_chessboard(name: str, payload: ChessboardUpdate) -> Chessboard:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Chessboard not found")
    current = _load_chessboard_from_disk(path)
    updated = current.model_copy(update=payload.model_dump(exclude_none=True))
    path.write_text(updated.model_dump_json(indent=2))
    return updated


# --- delete ------------------------------------------------------------------

@router_profiles.delete("/{name}", status_code=204)
@router_chessboards.delete("/{name}", status_code=204)
def delete_chessboard(name: str) -> None:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Chessboard not found")
    path.unlink()
