"""WebSocket client that connects to the calibration server and forwards JPEG frames.

Two connection modes:

  Automated (SSH-deployed, no --session-id):
      Connects to  /ws/agent?token=TOKEN
      Phase 1: sends  {"type": "cameras", "cameras": [...]}
      Phase 2: waits  {"type": "start", "camera_id": "..."}   from server
      Phase 3: streams binary JPEG frames

  Manual (hand-launched, --session-id provided):
      Connects to  /ws/remote/{session_id}?token=TOKEN
      Phase 1: sends  {"type": "cameras", "cameras": [...]}
      Phase 2: waits  {"type": "start"}                       from server
      Phase 3: streams binary JPEG frames using pre-configured camera_id
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import cv2
import websockets

from .camera import CameraCapture, list_local_cameras

log = logging.getLogger(__name__)


class AgentClient:
    def __init__(
        self,
        server_url: str,
        token: str,
        session_id: Optional[str] = None,
        camera_id: Optional[str] = None,
        fps: int = 15,
        jpeg_quality: int = 80,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.session_id = session_id   # None  → automated mode
        self.camera_id = camera_id     # None  → server picks
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _ws_url(self) -> str:
        if self.session_id:
            return f"{self.server_url}/ws/remote/{self.session_id}?token={self.token}"
        return f"{self.server_url}/ws/agent?token={self.token}"

    async def run(self) -> None:
        url = self._ws_url()
        log.info("Connecting to %s", url)
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            # --- Phase 1: announce available cameras ---
            cameras = list_local_cameras()
            await ws.send(json.dumps({"type": "cameras", "cameras": cameras}))
            log.info("Sent %d cameras", len(cameras))

            # --- Phase 2: wait for start command ---
            camera_id = self.camera_id
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=600.0)
                except asyncio.TimeoutError:
                    log.warning("Timed out waiting for start command")
                    return
                if isinstance(msg, str):
                    try:
                        cmd = json.loads(msg)
                    except Exception:
                        continue
                    if cmd.get("type") == "start":
                        # Server may override the camera even in manual mode
                        camera_id = cmd.get("camera_id") or camera_id or "0"
                        log.info("Starting stream from camera %s", camera_id)
                        break
                    if cmd.get("type") == "stop":
                        log.info("Server requested stop before stream")
                        return

            # --- Phase 3: stream frames ---
            cap = CameraCapture(camera_id, target_fps=self.fps)
            cap.open()
            send_task = asyncio.create_task(self._send_loop(ws, cap))
            recv_task = asyncio.create_task(self._recv_loop(ws))
            done, pending = await asyncio.wait(
                {send_task, recv_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            cap.close()

    async def _send_loop(self, ws, cap: CameraCapture) -> None:
        delay = 1.0 / max(self.fps, 1)
        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        while not self._stop.is_set():
            frame = cap.read()
            if frame is None:
                await asyncio.sleep(delay)
                continue
            ok, buf = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                continue
            try:
                await ws.send(buf.tobytes())
            except Exception as exc:
                log.warning("Send failed: %s", exc)
                self._stop.set()
                return
            await asyncio.sleep(delay)

    async def _recv_loop(self, ws) -> None:
        try:
            async for msg in ws:
                if isinstance(msg, str):
                    try:
                        cmd = json.loads(msg)
                    except Exception:
                        continue
                    if cmd.get("type") == "stop":
                        log.info("Server requested stop")
                        self._stop.set()
                        return
        except Exception as exc:
            log.warning("Recv loop ended: %s", exc)
            self._stop.set()
