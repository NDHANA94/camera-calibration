"""End-to-end camera-in-the-loop tests (GATED by `--e2e --camera=/dev/videoX`).

Skipped by default so CI stays green without a physical camera.

These tests don't try to push synthetic frames through the WebSocket — that
path is already exercised end-to-end by `test_api_calibrate_integration.py`
using the same session-directory contract. Instead, this module verifies
the things that REQUIRE a real device:

1. The server boots and `/health` returns ok.
2. `GET /cameras/{id}/probe` actually opens the V4L2 device and reports a
   real resolution.

Run with:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_e2e_camera.py \\
        --e2e --camera=/dev/video0 -v
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_http(url: str, timeout_s: float = 30.0) -> bool:
    import urllib.request
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def e2e_server(tmp_path_factory, pytestconfig):
    """Boot a real uvicorn server on a free port, pointed at tmp data dirs."""
    if not pytestconfig.getoption("--e2e"):
        pytest.skip("--e2e not passed")
    camera_path = pytestconfig.getoption("--camera")
    if not camera_path:
        pytest.skip("--camera=<path> required for E2E tests")

    data_root = tmp_path_factory.mktemp("e2e_data")
    (data_root / "sessions").mkdir()
    (data_root / "profiles").mkdir()

    port = _free_port()
    bootstrap = (
        "import os, sys;"
        "sys.path.insert(0, '.');"
        "from pathlib import Path;"
        "from server.core import storage as s;"
        f"s.SESSIONS_DIR = Path('{data_root}/sessions');"
        f"s.PROFILES_DIR = Path('{data_root}/profiles');"
        "s.DATA_DIR = s.SESSIONS_DIR.parent;"
        f"import uvicorn; uvicorn.run('server.app:app', host='127.0.0.1', port={port}, log_level='warning')"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        if not _wait_for_http(f"{base_url}/health", timeout_s=20.0):
            out, err = proc.communicate(timeout=2)
            pytest.fail(f"server failed to start: {err.decode(errors='replace')}")
        yield {"base_url": base_url, "camera": camera_path, "data_root": data_root}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_e2e_server_health_endpoint(e2e_server):
    """The boot path is the first thing that breaks when imports regress."""
    import httpx
    r = httpx.get(f"{e2e_server['base_url']}/health", timeout=5.0)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_e2e_camera_probe_returns_resolution(e2e_server):
    """Open the real V4L2 device and confirm it yields a frame with a valid
    resolution. This catches driver / udev / permission issues that the
    integration tests can't."""
    import httpx
    camera = e2e_server["camera"]
    r = httpx.get(f"{e2e_server['base_url']}/cameras/{camera}/probe", timeout=10.0)
    if r.status_code == 400:
        pytest.skip(f"camera {camera} not usable in this environment: {r.json().get('detail')}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == camera
    assert body["resolution"] and len(body["resolution"]) == 2
    w, h = body["resolution"]
    assert w > 0 and h > 0, f"invalid resolution: {body['resolution']}"


def test_e2e_cameras_list_includes_requested_device(e2e_server):
    """The enumeration endpoint should report the real camera we passed in."""
    import httpx
    camera = e2e_server["camera"]
    r = httpx.get(f"{e2e_server['base_url']}/cameras", timeout=5.0)
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()]
    # `/cameras` enumerates V4L2 + RTSP + Aravis; V4L2 ids are
    # "/dev/videoN" so just confirm the basename matches one of the entries.
    base = Path(camera).name
    assert any(c.get("id", "").endswith(base) for c in r.json()), \
        f"{camera!r} not in {ids}"