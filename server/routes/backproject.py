"""Back-projection test tool.

For a finished session we let the user enter an extrinsic pose, capture a live
camera shot, draw a bounding box over an object, and compute the 3D world ray
(origin + unit direction) through the bbox centre.

Endpoints:
    GET  /back-project/stream/{session_id}?camera_id=...&agent_id=...
            -- raw video relay (SSE, base64 JPEG).  No calibration pipeline and
               no capture saving: this must not mutate session data.  Local
               sessions open the camera directly; remote sessions proxy the
               agent's /ws/stream over the SSH tunnel.
    POST /back-project/{session_id}/compute
            -- given {rvec, tvec, bbox}, return {origin, direction} of the ray.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

import cv2
import numpy as np
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.backproject import back_project_ray, stereo_depth
from ..core.remote_link import agent_registry
from ..core.storage import session_dir
from ..core.ssh_deploy import tunnel_to_agent

router = APIRouter(prefix="/back-project", tags=["back-project"])
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class BBox(BaseModel):
    x: float
    y: float
    w: float
    h: float


class ComputePayload(BaseModel):
    rvec: List[float]   # Rodrigues rotation vector (world -> camera)
    tvec: List[float]   # translation (world -> camera)
    bbox: BBox


class DepthPayload(BaseModel):
    # Provide exactly one frame source:
    image: Optional[str] = None   # base64 JPEG of the side-by-side L|R frame (live)
    frame: Optional[str] = None   # filename of a saved capture under frames/
    bbox: Optional[BBox] = None   # optional, in left-eye pixel coords


# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

def _load_intrinsics(session_id: str):
    """Return (K, dist) for the session's camera.  Stereo sessions use the
    left camera (K1/D1)."""
    sdir = session_dir(session_id)
    npz_path = sdir / "result.npz"
    if not npz_path.exists():
        raise HTTPException(404, "No calibration result — run calibration first")
    data = np.load(str(npz_path), allow_pickle=True)
    if "stereo" in data.files and bool(data["stereo"]):
        return np.asarray(data["K1"]), np.asarray(data["D1"])
    return np.asarray(data["camera_matrix"]), np.asarray(data["dist_coeffs"])


@router.post("/{session_id}/compute")
def compute(session_id: str, payload: ComputePayload) -> dict:
    if len(payload.rvec) != 3 or len(payload.tvec) != 3:
        raise HTTPException(400, "rvec and tvec must each have 3 elements")
    K, dist = _load_intrinsics(session_id)
    uv = (payload.bbox.x + payload.bbox.w / 2.0,
          payload.bbox.y + payload.bbox.h / 2.0)
    origin, direction = back_project_ray(K, dist, uv, payload.rvec, payload.tvec)
    return {
        "origin": [float(v) for v in origin],
        "direction": [float(v) for v in direction],
        "bbox_center": [float(uv[0]), float(uv[1])],
    }


def _load_stereo_calib(session_id: str) -> dict:
    """Load the stereo rectification data, or 400 if the session is mono."""
    sdir = session_dir(session_id)
    npz_path = sdir / "result.npz"
    if not npz_path.exists():
        raise HTTPException(404, "No calibration result — run calibration first")
    data = np.load(str(npz_path), allow_pickle=True)
    if "stereo" not in data.files or not bool(data["stereo"]):
        raise HTTPException(400, "Depth is only available for stereo sessions")
    size = [int(x) for x in data["image_size"].tolist()]  # per-eye (w, h)
    return {
        "K1": np.asarray(data["K1"]), "D1": np.asarray(data["D1"]),
        "K2": np.asarray(data["K2"]), "D2": np.asarray(data["D2"]),
        "R1": np.asarray(data["R1"]), "R2": np.asarray(data["R2"]),
        "P1": np.asarray(data["P1"]), "P2": np.asarray(data["P2"]),
        "Q": np.asarray(data["Q"]),
        "size": (size[0], size[1]),
    }


@router.post("/{session_id}/depth")
def depth(session_id: str, payload: DepthPayload) -> dict:
    calib = _load_stereo_calib(session_id)
    frame = None
    if payload.frame:
        # Saved calibration capture.  Guard against path traversal.
        name = Path(payload.frame).name
        fpath = session_dir(session_id) / "frames" / name
        if not fpath.exists():
            raise HTTPException(404, "Captured frame not found")
        frame = cv2.imread(str(fpath), cv2.IMREAD_COLOR)
    elif payload.image:
        try:
            raw = base64.b64decode(payload.image)
            arr = np.frombuffer(raw, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            frame = None
    if frame is None:
        raise HTTPException(400, "Could not load the frame (provide 'image' or 'frame')")
    center = None
    if payload.bbox is not None:
        center = (payload.bbox.x + payload.bbox.w / 2.0,
                  payload.bbox.y + payload.bbox.h / 2.0)
    result = stereo_depth(calib, session_id, frame, bbox_center_left=center)
    return {
        "heatmap": base64.b64encode(result["heatmap_jpeg"]).decode(),
        "depth_mm": result["depth_mm"],
        "min_mm": result["min_mm"],
        "max_mm": result["max_mm"],
        "lo_mm": result["lo_mm"],
        "hi_mm": result["hi_mm"],
    }


# ---------------------------------------------------------------------------
# Raw video relay (SSE)
# ---------------------------------------------------------------------------

def _session_source(session_id: str) -> str:
    sdir = session_dir(session_id)
    sfile = sdir / "session.json"
    if not sfile.exists():
        raise HTTPException(404, "Session not found")
    try:
        return json.loads(sfile.read_text()).get("source", "local")
    except Exception:
        return "local"


async def _local_relay(camera_id: str):
    """Yield SSE frames from a locally-attached camera (no pipeline)."""
    from ..core.camera_manager import CameraCapture

    cap = CameraCapture(camera_id, target_fps=15)
    try:
        cap.open()
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        return
    yield f"data: {json.dumps({'type': 'started', 'camera': camera_id})}\n\n"
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
    try:
        while True:
            frame = cap.read()
            if frame is None:
                await asyncio.sleep(1.0 / 15)
                continue
            ok, buf = cv2.imencode(".jpg", frame, encode_params)
            if ok:
                yield (f"data: {json.dumps({'type': 'frame', 'data': base64.b64encode(buf.tobytes()).decode()})}\n\n")
            await asyncio.sleep(1.0 / 15)
    except asyncio.CancelledError:
        raise
    finally:
        cap.close()


async def _local_stereo_relay(camera_left: str, camera_right: str):
    """Yield SSE side-by-side L|R frames from two local cameras (no pipeline)."""
    from ..core.camera_manager import CameraCapture

    capL = CameraCapture(camera_left, target_fps=15)
    capR = CameraCapture(camera_right, target_fps=15)
    try:
        capL.open()
        capR.open()
    except Exception as exc:
        try: capL.close()
        except Exception: pass
        try: capR.close()
        except Exception: pass
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        return
    yield f"data: {json.dumps({'type': 'started', 'camera': camera_left, 'dual': True})}\n\n"
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
    last_right = None
    try:
        while True:
            fL = capL.read()
            fR = capR.read()
            if fL is None:
                await asyncio.sleep(1.0 / 15)
                continue
            if fR is not None:
                last_right = fR
            fR_send = last_right if last_right is not None else fL
            if fR_send.shape[0] != fL.shape[0]:
                scale = fL.shape[0] / float(fR_send.shape[0])
                fR_send = cv2.resize(
                    fR_send, (int(round(fR_send.shape[1] * scale)), fL.shape[0]),
                    interpolation=cv2.INTER_AREA)
            combined = np.hstack([fL, fR_send])
            ok, buf = cv2.imencode(".jpg", combined, encode_params)
            if ok:
                yield (f"data: {json.dumps({'type': 'frame', 'data': base64.b64encode(buf.tobytes()).decode()})}\n\n")
            await asyncio.sleep(1.0 / 15)
    except asyncio.CancelledError:
        raise
    finally:
        capL.close()
        capR.close()


async def _remote_relay(agent_id: str, ws_path: str, camera_label: str):
    """Yield SSE frames proxied from the remote agent's *ws_path* (no pipeline).

    *ws_path* is the agent endpoint + query (``/ws/stream?...`` for mono/stereo_lr,
    ``/ws/stream_stereo?...`` for stereo_separate)."""
    state = agent_registry.get(agent_id)
    if state is None:
        yield f"data: {json.dumps({'type': 'error', 'message': 'Remote agent not connected.'})}\n\n"
        return
    try:
        reader, writer, closer = await tunnel_to_agent(state.ssh_conn, state.agent_port)
    except Exception as exc:
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        return
    try:
        key = base64.b64encode(hashlib.md5(str(id(reader)).encode()).digest()).decode()
        url = ws_path
        req = (
            f"GET {url} HTTP/1.1\r\n"
            f"Host: x\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        writer.write(req)
        await writer.drain()

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
            if not chunk:
                raise ConnectionError("agent closed before sending upgrade response")
            head += chunk
        if b"101" not in head.split(b"\r\n", 1)[0]:
            yield f"data: {json.dumps({'type': 'error', 'message': 'agent upgrade failed'})}\n\n"
            return
        yield f"data: {json.dumps({'type': 'started', 'camera': camera_label})}\n\n"

        # Pump WS frames, keep only the newest JPEG (drop backlog), relay it.
        box = {"jpeg": None, "done": False}
        new_data = asyncio.Event()

        async def pump(buf: bytes):
            try:
                while True:
                    while len(buf) < 2:
                        c = await reader.read(4096)
                        if not c:
                            return
                        buf += c
                    b1, b2 = buf[0], buf[1]
                    opcode = b1 & 0x0F
                    n = b2 & 0x7F
                    idx = 2
                    if n == 126:
                        while len(buf) < idx + 2:
                            buf += await reader.read(4096)
                        n = struct.unpack(">H", buf[idx:idx + 2])[0]
                        idx += 2
                    elif n == 127:
                        while len(buf) < idx + 8:
                            buf += await reader.read(4096)
                        n = struct.unpack(">Q", buf[idx:idx + 8])[0]
                        idx += 8
                    masked = b2 & 0x80
                    if masked:
                        while len(buf) < idx + 4:
                            buf += await reader.read(4096)
                        mask = buf[idx:idx + 4]
                        idx += 4
                    else:
                        mask = b""
                    while len(buf) < idx + n:
                        c = await reader.read(4096)
                        if not c:
                            return
                        buf += c
                    payload = bytes(buf[idx:idx + n])
                    if mask:
                        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                    buf = buf[idx + n:]
                    if opcode == 0x8:  # close
                        return
                    if opcode == 0x9:  # ping
                        continue
                    if opcode == 0x2:  # binary JPEG -- keep newest only
                        box["jpeg"] = payload
                        new_data.set()
            finally:
                box["done"] = True
                new_data.set()

        pump_task = asyncio.create_task(pump(head.split(b"\r\n\r\n", 1)[1]))
        try:
            while True:
                await new_data.wait()
                new_data.clear()
                jpeg = box["jpeg"]
                box["jpeg"] = None
                if jpeg is not None:
                    yield (f"data: {json.dumps({'type': 'frame', 'data': base64.b64encode(jpeg).decode()})}\n\n")
                if box["done"] and box["jpeg"] is None:
                    return
        finally:
            pump_task.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("back-project remote relay error: %s", exc)
        yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
    finally:
        try: writer.close()
        except Exception: pass
        try: await closer()
        except Exception: pass


@router.get("/stream/{session_id}")
async def stream(session_id: str, camera_id: str,
                 camera_id_2: Optional[str] = None,
                 agent_id: Optional[str] = None) -> StreamingResponse:
    """Raw video relay.  When *camera_id_2* is given (stereo_separate) the two
    cameras are combined into a side-by-side L|R frame so the client can both
    draw a bbox (left half) and request a depth map."""
    source = _session_source(session_id)
    if source == "remote":
        if not agent_id:
            raise HTTPException(400, "agent_id required for remote sessions")
        if camera_id_2:
            ws_path = (
                f"/ws/stream_stereo?camera_left={quote(camera_id, safe='')}"
                f"&camera_right={quote(camera_id_2, safe='')}&fps=15&quality=80"
            )
        else:
            ws_path = f"/ws/stream?camera={quote(camera_id, safe='')}&fps=15&quality=80"
        gen = _remote_relay(agent_id, ws_path, camera_id)
    else:
        if camera_id_2:
            gen = _local_stereo_relay(camera_id, camera_id_2)
        else:
            gen = _local_relay(camera_id)
    return StreamingResponse(gen, media_type="text/event-stream")
