"""WebSocket streaming — local camera, legacy manual-remote, and new SSH-automated remote.

Browser wire protocol  (/ws/local/{sid}  and  /ws/watch/{sid}):
    server → browser : binary  JPEG
    server → browser : text    JSON  {type: "capture"|"hint"|"status"|"error"|"finished"}
    browser → server : text    JSON  {type: "capture_now"|"stop"|"abort"}

Agent wire protocol  (/ws/agent?token=TOKEN):
    Phase 1 — discovery:
        agent  → server : text   JSON  {"type": "cameras", "cameras": [...]}
        server → agent  : text   JSON  {"type": "start", "camera_id": "..."}
    Phase 2 — streaming:
        agent  → server : binary JPEG frames
        server → agent  : text   JSON  {"type": "stop"}   (on abort)

Legacy agent protocol  (/ws/remote/{sid}?token=TOKEN):
    agent  → server : text   JSON  {"type": "cameras", "cameras": [...]}
    server → agent  : text   JSON  {"type": "start"}
    agent  → server : binary JPEG frames
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..core.camera_manager import CameraCapture
from ..core.guidance import coverage_tip
from ..core.remote_link import agent_registry, registry
from ..core.runtime import drop_runtime, get_runtime
from ..models.schemas import SessionSource

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _send_json(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


async def _process_and_dispatch(
    runtime,
    frame_bgr: np.ndarray,
    *,
    out_ws: Optional[WebSocket] = None,
    force_capture: bool = False,
) -> None:
    """Process one frame through the per-session pipeline.

    Saves corners to disk. If *out_ws* is given, also sends the JPEG and
    event messages to it (the browser). When *out_ws* is None the frame is
    still processed/saved — it just isn't streamed anywhere.
    """
    from ..core.sessions_helpers import bump_capture
    from ..core.storage import session_dir

    force = force_capture or getattr(runtime, "force_capture", False)
    if getattr(runtime, "force_capture", False):
        runtime.force_capture = False

    from ..core.frame_pipeline import FrameResult  # noqa: F401  (type hint only)
    result = runtime.pipeline.process(
        frame_bgr,
        hint_fn=lambda caps: coverage_tip(caps),
        force_capture=force,
    )
    if result.capture_taken and runtime.image_size is None:
        runtime.image_size = (result.width, result.height)

    # Stream JPEG to browser (if watching)
    if out_ws is not None:
        try:
            await out_ws.send_bytes(result.jpeg)
        except Exception:
            out_ws = None  # viewer disconnected — keep processing

    sdir = session_dir(runtime.session_id)
    if runtime.image_size is None:
        runtime.image_size = (result.width, result.height)
        (sdir / "image_size.json").write_text(
            json.dumps({"image_size": list(runtime.image_size)})
        )

    if result.capture_taken:
        corners_dir = sdir / "corners"
        corners_dir.mkdir(parents=True, exist_ok=True)
        idx = len(list(corners_dir.glob("*.npy")))
        np.save(corners_dir / f"{idx:04d}.npy", result.corners)
        try:
            cv2.imwrite(str(sdir / "frames" / f"{idx:04d}.png"), frame_bgr)
        except Exception:
            pass
        runtime.captures_count += 1
        bump_capture(runtime.session_id)
        if out_ws is not None:
            await _send_json(out_ws, {
                "type": "capture",
                "n": runtime.captures_count,
                "blur": round(result.blur_score, 1),
            })

    runtime._frame_count = getattr(runtime, "_frame_count", 0) + 1
    fc = runtime._frame_count

    if out_ws is not None:
        if fc % 10 == 0:
            await _send_json(out_ws, {
                "type": "status",
                "board": result.board_found,
                "blur": round(result.blur_score, 1),
            })
        if fc % 60 == 0 or result.capture_taken:
            await _send_json(out_ws, {
                "type": "hint",
                "message": coverage_tip(runtime.pipeline.captures),
            })


async def _recv_commands(ws: WebSocket, runtime) -> None:
    """Read JSON commands from *ws* until disconnect."""
    while True:
        try:
            msg = await ws.receive_text()
        except (WebSocketDisconnect, Exception):
            return
        try:
            data = json.loads(msg)
        except Exception:
            continue
        kind = data.get("type")
        if kind in ("stop", "abort"):
            runtime.aborted = True
            return
        if kind == "capture_now":
            runtime.force_capture = True


# ---------------------------------------------------------------------------
# Local camera loop
# ---------------------------------------------------------------------------

async def _local_loop(ws: WebSocket, session_id: str, camera_id: str) -> None:
    runtime = await get_runtime(session_id)
    cap = CameraCapture(camera_id)
    try:
        cap.open()
    except Exception as exc:
        await _send_json(ws, {"type": "error", "message": str(exc)})
        await ws.close()
        return

    cmd_task = asyncio.create_task(_recv_commands(ws, runtime))
    try:
        while True:
            if getattr(runtime, "aborted", False):
                break
            frame = cap.read()
            if frame is None:
                await asyncio.sleep(0.02)
                continue
            await _process_and_dispatch(runtime, frame, out_ws=ws)
            await asyncio.sleep(0.0)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Local stream error: %s", exc)
        await _send_json(ws, {"type": "error", "message": str(exc)})
    finally:
        cmd_task.cancel()
        cap.close()
        await drop_runtime(session_id)


# ---------------------------------------------------------------------------
# Legacy manual-remote loop  (/ws/remote/{session_id})
# ---------------------------------------------------------------------------

async def _remote_loop(ws: WebSocket, session_id: str) -> None:
    """Handles a manually-launched agent connecting to /ws/remote/{session_id}.

    Protocol update: agent first sends a camera list; server replies with
    {"type": "start"} so the agent begins streaming.
    """
    runtime = await get_runtime(session_id)
    agent = registry.get(session_id)

    # Expect {"type": "cameras", "cameras": [...]} from agent
    try:
        msg = await ws.receive()
    except Exception:
        registry.unregister(session_id)
        await drop_runtime(session_id)
        return
    if msg.get("type") == "websocket.disconnect":
        registry.unregister(session_id)
        await drop_runtime(session_id)
        return
    if "text" in msg and msg["text"]:
        try:
            data = json.loads(msg["text"])
            if data.get("type") == "cameras" and agent:
                agent.cameras = data.get("cameras", [])
        except Exception:
            pass

    # Acknowledge — agent streams from its pre-configured camera
    await _send_json(ws, {"type": "start"})

    cmd_task = asyncio.create_task(_recv_commands(ws, runtime))
    try:
        while True:
            if getattr(runtime, "aborted", False):
                await _send_json(ws, {"type": "stop"})
                break
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "bytes" in msg and msg["bytes"]:
                arr = np.frombuffer(msg["bytes"], dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                # No browser viewer for legacy flow — still saves captures
                await _process_and_dispatch(runtime, frame, out_ws=None)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Legacy remote stream error: %s", exc)
    finally:
        cmd_task.cancel()
        registry.unregister(session_id)
        await drop_runtime(session_id)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Legacy REST: issue token for manual agent launch
# ---------------------------------------------------------------------------

from fastapi import Request  # noqa: E402


@router.post("/remote/{session_id}/token")
async def issue_remote_token(session_id: str, request: Request):
    from ..models.schemas import RemoteTokenResponse
    from .sessions import get_session

    info = get_session(session_id)
    if info.source != SessionSource.REMOTE:
        raise HTTPException(400, "Session is not configured for remote source")
    token = registry.issue_token(session_id)
    host = request.headers.get("host", "127.0.0.1:8000")
    scheme = "wss" if request.url.scheme == "https" else "ws"
    cmd = (
        f"sintez-cam-agent "
        f"--server {scheme}://{host} "
        f"--token {token} "
        f"--session-id {session_id}"
    )
    return RemoteTokenResponse(
        token=token,
        server_url=f"{scheme}://{host}/ws/remote/{session_id}?token={token}",
        session_id=session_id,
        agent_command=cmd,
    )
