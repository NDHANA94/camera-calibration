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
) -> bool:
    """Process one frame through the per-session pipeline (mono or stereo).

    Saves corners to disk via the shared core. If *out_ws* is given, also
    sends the annotated JPEG and event messages to it (the browser).

    Returns False if the viewer socket failed (disconnected) so the caller can
    stop the loop and release the camera; True otherwise."""
    from ..core.stream_processing import process_frame

    jpeg, events = process_frame(runtime, frame_bgr, force_capture=force_capture)

    if out_ws is not None:
        try:
            await out_ws.send_bytes(jpeg)
            for ev in events:
                await out_ws.send_text(json.dumps(ev))
        except Exception:
            return False  # viewer disconnected
    return True


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
            # Stop (and release the camera) on abort or when the browser
            # disconnects -- otherwise the loop would spin forever holding the
            # camera, which also blocks camera enumeration for everyone else.
            if getattr(runtime, "aborted", False) or cmd_task.done():
                break
            frame = cap.read()
            if frame is None:
                await asyncio.sleep(0.02)
                continue
            ok = await _process_and_dispatch(runtime, frame, out_ws=ws)
            if not ok:
                break  # viewer disconnected
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


async def _local_dual_loop(
    ws: WebSocket,
    session_id: str,
    camera_id_left: str,
    camera_id_right: str,
) -> None:
    """Local loop for STEREO_SEPARATE: open two cameras, sync their frames,
    feed the (left, right) pair into :class:`DualCameraPipeline`."""
    runtime = await get_runtime(session_id)
    capL = CameraCapture(camera_id_left)
    capR = CameraCapture(camera_id_right)
    try:
        capL.open()
        capR.open()
    except Exception as exc:
        try: capL.close()
        except Exception: pass
        try: capR.close()
        except Exception: pass
        await _send_json(ws, {"type": "error", "message": str(exc)})
        await ws.close()
        return

    cmd_task = asyncio.create_task(_recv_commands(ws, runtime))
    try:
        while True:
            if getattr(runtime, "aborted", False) or cmd_task.done():
                break
            fL = capL.read()
            fR = capR.read()
            # If either side is missing, skip this round; the dual pipeline
            # also degrades gracefully when given a None pair.
            if fL is None or fR is None:
                await asyncio.sleep(0.02)
                continue
            ok = await _process_and_dispatch(runtime, (fL, fR), out_ws=ws)
            if not ok:
                break
            await asyncio.sleep(0.0)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.exception("Local dual stream error: %s", exc)
        await _send_json(ws, {"type": "error", "message": str(exc)})
    finally:
        cmd_task.cancel()
        capL.close()
        capR.close()
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
# WebSocket routes
# ---------------------------------------------------------------------------

@router.websocket("/ws/local/{session_id}")
async def ws_local(ws: WebSocket, session_id: str) -> None:
    """Local-camera streaming.  The browser opens this; the server reads the
    local camera, runs the pipeline, and streams annotated JPEGs back.

    For sessions whose chessboard mode is STEREO_SEPARATE this dispatches to
    the dual-camera loop, which opens TWO cameras and feeds paired frames
    into :class:`DualCameraPipeline`."""
    await ws.accept()
    from .sessions import get_session
    try:
        info = get_session(session_id)
        camera_id = info.camera_id
        camera_id_2 = info.camera_id_2
    except Exception:
        await _send_json(ws, {"type": "error", "message": "Session not found"})
        await ws.close()
        return
    if not camera_id:
        await _send_json(ws, {"type": "error", "message": "No camera configured for this session"})
        await ws.close()
        return
    # Inspect the chessboard mode to pick mono / dual.
    try:
        from ..models.schemas import CameraMode
        from ..core.sessions_helpers import load_chessboard_for_session
        cb = load_chessboard_for_session(session_id)
        if cb.mode == CameraMode.STEREO_SEPARATE and camera_id_2:
            await _local_dual_loop(ws, session_id, camera_id, camera_id_2)
            return
    except Exception:
        pass
    await _local_loop(ws, session_id, camera_id)


@router.websocket("/ws/remote/{session_id}")
async def ws_remote(ws: WebSocket, session_id: str) -> None:
    """Legacy manual-agent streaming: an agent the user launched by hand
    connects here with a one-time token."""
    token = ws.query_params.get("token", "")
    sid = registry.consume(token) if token else None
    if not sid or sid != session_id:
        await ws.close(code=1008)
        return
    await ws.accept()
    registry.register(session_id, ws)
    await _remote_loop(ws, session_id)


# ---------------------------------------------------------------------------
# Legacy REST: issue token for manual agent launch
# ---------------------------------------------------------------------------

from fastapi import Request  # noqa: E402


@router.post("/sessions/{session_id}/capture-now")
async def capture_now_rest(session_id: str) -> dict:
    """Force-capture the next good frame.  Used by the remote (SSE) flow,
    whose one-way stream can't carry a WebSocket ``capture_now`` command."""
    from ..core.runtime import get_runtime
    runtime = await get_runtime(session_id)
    runtime.force_capture = True
    return {"ok": True}


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
