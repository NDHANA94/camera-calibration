"""Shared pytest fixtures.

Key principles:
- Tests never touch real session/profile data. `core.storage.SESSIONS_DIR` and
  `PROFILES_DIR` are monkeypatched to a `tmp_path` per test.
- The FastAPI app uses `from .core.storage import SESSIONS_DIR` at import
  time. We patch the module attributes *before* creating the TestClient so
  both the storage helpers and the static-file mounts see the tmp dir.
- The TestClient fixture also resets the in-memory session index
  (`routes.sessions._INDEX`) so tests don't leak state between runs.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest

# Make `server` importable when pytest is invoked from the repo root without
# installing the package. Most CI installs via `pip install -e .`, but this
# makes `pytest tests/` from a fresh checkout work too.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# CLI flag for E2E tests
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--e2e", action="store_true", default=False,
        help="Run end-to-end camera-in-the-loop tests (requires a real camera).",
    )
    parser.addoption(
        "--camera", action="store", default=None,
        help="Path to a V4L2 device, e.g. /dev/video0, for E2E tests.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip e2e-marked tests unless --e2e is passed."""
    if config.getoption("--e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="E2E tests require --e2e flag (and --camera=...)")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_e2e)


# ---------------------------------------------------------------------------
# Isolated data directories + TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect server.core.storage to a per-test tmp directory.

    Both the storage module's attributes AND the SESSIONS_DIR object
    referenced by app.mount(...) must point at the new location, since
    StaticFiles(directory=str(SESSIONS_DIR)) captures the value at import
    time. We reimport `server.app` after patching so the mount uses the tmp
    directory.
    """
    sessions_dir = tmp_path / "sessions"
    profiles_dir = tmp_path / "profiles"
    sessions_dir.mkdir()
    profiles_dir.mkdir()

    from server.core import storage as storage_mod
    monkeypatch.setattr(storage_mod, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(storage_mod, "PROFILES_DIR", profiles_dir)
    monkeypatch.setattr(storage_mod, "DATA_DIR", tmp_path)

    # Reset the in-memory session index between tests
    from server.routes import sessions as sessions_mod
    sessions_mod._INDEX.clear()

    # Reimport the app so StaticFiles(SESSIONS_DIR) captures the new path.
    # If the app was already imported, drop it from sys.modules and reload.
    if "server.app" in sys.modules:
        # Touch the module so the StaticFiles mount is rebuilt against the new dir
        del sys.modules["server.app"]
    from server.app import app  # noqa: F401

    return {"sessions": sessions_dir, "profiles": profiles_dir, "root": tmp_path}


@pytest.fixture
def http_client(tmp_data_dirs):
    """FastAPI TestClient bound to the tmp-data-dir app."""
    from starlette.testclient import TestClient
    from server.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Synthetic-camera fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_cam():
    """Default mid-range synthetic camera, deterministic."""
    from tests._synthetic import default_synthetic_camera
    return default_synthetic_camera()


@pytest.fixture
def rng():
    return np.random.default_rng(1234)