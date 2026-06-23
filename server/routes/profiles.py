"""CRUD for saved calibration profiles stored under server/data/profiles/."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from ..core.storage import profile_path
from ..models.schemas import Profile, ProfileCreate, ProfileUpdate

router = APIRouter(prefix="/profiles", tags=["profiles"])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@router.get("", response_model=List[Profile])
def list_profiles() -> List[Profile]:
    out: List[Profile] = []
    for p in sorted(Path(profile_path("__nope__")).parent.glob("*.json")):
        try:
            data = json.loads(p.read_text())
            out.append(Profile(**data))
        except Exception:
            continue
    return out


@router.post("", response_model=Profile, status_code=201)
def create_profile(payload: ProfileCreate) -> Profile:
    if not _SAFE_NAME.match(payload.name):
        raise HTTPException(400, "Invalid profile name")
    path = profile_path(payload.name)
    if path.exists():
        raise HTTPException(409, "Profile already exists")
    profile = Profile(**payload.model_dump())
    path.write_text(profile.model_dump_json(indent=2))
    return profile


@router.post("/save", response_model=Profile)
def save_profile(payload: ProfileCreate) -> Profile:
    """Create-or-overwrite a profile. Used by the UI's 'Save' button."""
    if not _SAFE_NAME.match(payload.name):
        raise HTTPException(
            400,
            f"Invalid profile name '{payload.name}'. "
            "Use only letters, digits, underscores, hyphens, or dots (no spaces).",
        )
    path = profile_path(payload.name)
    profile = Profile(**payload.model_dump())
    path.write_text(profile.model_dump_json(indent=2))
    return profile


@router.get("/{name}", response_model=Profile)
def get_profile(name: str) -> Profile:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Profile not found")
    return Profile(**json.loads(path.read_text()))


@router.put("/{name}", response_model=Profile)
def update_profile(name: str, payload: ProfileUpdate) -> Profile:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Profile not found")
    current = Profile(**json.loads(path.read_text()))
    updated = current.model_copy(update=payload.model_dump(exclude_none=True))
    path.write_text(updated.model_dump_json(indent=2))
    return updated


@router.delete("/{name}", status_code=204)
def delete_profile(name: str) -> None:
    path = profile_path(name)
    if not path.exists():
        raise HTTPException(404, "Profile not found")
    path.unlink()