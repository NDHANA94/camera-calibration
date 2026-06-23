"""Minimal HTTP + WebSocket server for the remote agent.

Exposes:
    GET  /info          -> JSON  {"host": str, "version": str, "pid": int}
    GET  /cameras       -> JSON  {"cameras": [{id, label}, ...]}
    GET  /healthz       -> text/plain "ok"

    GET  /ws/stream     -> upgrades to WebSocket, streams JPEG frames
                           Query params: camera, fps, quality
    GET  /ws/logs       -> upgrades to WebSocket, streams log lines

Why no FastAPI? The agent runs on a small embedded box (Jetson / Orin NX).
We want the install footprint to be just opencv + numpy + websockets, no
need to drag a full ASGI stack.  The protocol above is small enough to
implement in a couple hundred lines of asyncio.

The server is reached from the calibration server via a `direct-tcpip`
SSH channel (see server.core.ssh_deploy.tunnel_to_agent) — so it only
listens on 127.0.0.1 and is never exposed to the network.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import struct
import sys
import time
from typing import Optional

import cv2

from .camera import CameraCapture, list_local_cameras

log = logging.getLogger(__name__)

AGENT_VERSION = "0.2.2"


# ---------------------------------------------------------------------------
# Tiny WebSocket server (RFC 6455, server side, no TLS, no extensions)
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + _WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


async def _ws_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict:
    """Read HTTP request headers, return parsed dict. Does not yet reply.

    Caller inspects headers (esp. ``Sec-WebSocket-Key``) and calls
    :func:`_ws_handshake_reply`.
    """
    headers: dict[str, str] = {}
    request_line = await reader.readline()
    if not request_line:
        raise ConnectionError("client closed before sending request")
    headers["_request_line"] = request_line.decode(errors="replace").rstrip("\r\n")
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"", b"\n"):
            break
        k, _, v = line.decode(errors="replace").rstrip("\r\n").partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers


def _ws_handshake_reply(headers: dict) -> bytes:
    key = headers.get("sec-websocket-key", "")
    accept = _ws_accept_key(key)
    return (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
    )


def _ws_send_text(writer: asyncio.StreamWriter, text: str) -> None:
    data = text.encode()
    # FIN=1, opcode=1 (text).  Small frames, no fragmentation.
    # Server->client frames are NOT masked (RFC 6455 §5.1), so the length
    # byte must NOT set the 0x80 mask bit -- doing so makes the client treat
    # the first 4 payload bytes as a mask key and corrupts the stream.
    header = bytes([0x81])
    n = len(data)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    writer.write(header + data)


def _ws_send_bytes(writer: asyncio.StreamWriter, payload: bytes) -> None:
    # FIN=1, opcode=2 (binary), unmasked (see _ws_send_text).
    header = bytes([0x82])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    writer.write(header + payload)


async def _ws_send_text_async(writer: asyncio.StreamWriter, text: str) -> None:
    _ws_send_text(writer, text)
    await writer.drain()


async def _ws_send_bytes_async(writer: asyncio.StreamWriter, payload: bytes) -> None:
    _ws_send_bytes(writer, payload)
    await writer.drain()


async def _ws_recv_text(reader: asyncio.StreamReader) -> Optional[str]:
    """Read one text frame from a client. Returns None on close."""
    hdr = await reader.readexactly(2)
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    if opcode == 0x8:
        return None  # close
    if opcode == 0x9:
        # ping → pong with same payload (we don't expect any)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await reader.readexactly(8))[0]
        await reader.readexactly(n)
        return ""  # signal to loop
    masked = b2 & 0x80
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    body = await reader.readexactly(n)
    if mask:
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
    if opcode == 0x1:
        return body.decode(errors="replace")
    return ""


# ---------------------------------------------------------------------------
# HTTP routing (minimal — request-line dispatch)
# ---------------------------------------------------------------------------

async def _send_http(writer: asyncio.StreamWriter, status: int, body: bytes,
                     content_type: str = "application/json") -> None:
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found",
              426: "Upgrade Required", 500: "Internal Server Error"}.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    writer.write(head + body)
    try:
        await writer.drain()
    except Exception:
        pass


def _parse_query(path: str) -> dict:
    if "?" not in path:
        return {}
    from urllib.parse import parse_qs
    flat = {}
    for k, vs in parse_qs(path.split("?", 1)[1]).items():
        flat[k] = vs[0]
    return flat


# ---------------------------------------------------------------------------
# Per-stream log buffer (for /ws/logs)
# ---------------------------------------------------------------------------

class LogBroker:
    """Ring buffer of recent log lines, with live tailing for subscribers."""

    def __init__(self, capacity: int = 2000) -> None:
        self._buf: list[str] = []
        self._subs: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    def publish(self, line: str) -> None:
        self._buf.append(line)
        if len(self._buf) > 2000:
            self._buf = self._buf[-2000:]
        for q in list(self._subs):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> tuple[list[str], asyncio.Queue]:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=2000)
            self._subs.add(q)
            return list(self._buf), q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.discard(q)


_log = LogBroker()


class _LogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _log.publish(msg)
        except Exception:
            pass


def install_log_broker() -> None:
    h = _LogHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.addHandler(h)
    # Also make sure the asyncio logger's "Using selector" doesn't spam clients
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Frame streaming
# ---------------------------------------------------------------------------

async def _stream_loop(ws_writer: asyncio.StreamReader,  # unused, kept for symmetry
                       writer: asyncio.StreamWriter,
                       camera_id: str,
                       fps: int,
                       quality: int) -> None:
    cap = CameraCapture(camera_id, target_fps=fps)
    try:
        cap.open()
    except Exception as exc:
        _ws_send_text(writer, json.dumps({"type": "error", "message": str(exc)}))
        await writer.drain()
        return

    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    delay = 1.0 / max(fps, 1)
    _ws_send_text(writer, json.dumps({
        "type": "started", "camera": camera_id, "fps": fps, "quality": quality,
    }))
    await writer.drain()

    try:
        while True:
            frame = cap.read()
            if frame is None:
                await asyncio.sleep(delay)
                continue
            ok, buf = cv2.imencode(".jpg", frame, encode_params)
            if not ok:
                continue
            try:
                _ws_send_bytes(writer, buf.tobytes())
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
                break
            await asyncio.sleep(delay)
    finally:
        cap.close()


# ---------------------------------------------------------------------------
# Connection handler
# ---------------------------------------------------------------------------

async def _handle_connection(reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log.debug("connection from %r", peer)
    try:
        headers = await _ws_handshake(reader, writer)
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        log.debug("handshake failed: %s", exc)
        try: writer.close()
        except Exception: pass
        return

    req = headers.get("_request_line", "")
    try:
        method, path, _ = req.split(" ", 2)
    except ValueError:
        await _send_http(writer, 400, b"bad request")
        writer.close()
        return

    # --- Plain HTTP endpoints ----------------------------------------------
    if "upgrade" not in headers:
        if method == "GET" and path == "/info":
            payload = json.dumps({
                "host": socket.gethostname(),
                "version": AGENT_VERSION,
                "pid": os.getpid(),
                "python": sys.version.split()[0],
            }).encode()
            await _send_http(writer, 200, payload)
        elif method == "GET" and path == "/cameras":
            # Enumeration probes devices via subprocesses and can take a few
            # seconds; run it off the event loop so other requests aren't
            # blocked meanwhile.
            cams = await asyncio.get_event_loop().run_in_executor(
                None, list_local_cameras
            )
            payload = json.dumps({"cameras": cams}).encode()
            await _send_http(writer, 200, payload)
        elif method == "GET" and path == "/healthz":
            await _send_http(writer, 200, b"ok", content_type="text/plain")
        else:
            await _send_http(writer, 404, b"not found", content_type="text/plain")
        try: await writer.drain()
        except Exception: pass
        writer.close()
        return

    # --- WebSocket upgrade --------------------------------------------------
    if headers.get("upgrade", "").lower() != "websocket":
        await _send_http(writer, 400, b"expected Upgrade: websocket")
        writer.close()
        return

    if method == "GET" and path.startswith("/ws/stream"):
        writer.write(_ws_handshake_reply(headers))
        await writer.drain()
        q = _parse_query(path)
        camera_id = q.get("camera", "0")
        fps = int(q.get("fps", "15"))
        quality = int(q.get("quality", "80"))
        log.info("stream start: camera=%s fps=%d q=%d", camera_id, fps, quality)
        await _stream_loop(reader, writer, camera_id, fps, quality)
        writer.close()
        return

    if method == "GET" and path.startswith("/ws/logs"):
        writer.write(_ws_handshake_reply(headers))
        await writer.drain()
        history, queue = await _log.subscribe()
        try:
            for line in history[-200:]:
                _ws_send_text(writer, line)
            await writer.drain()
            while True:
                line = await queue.get()
                _ws_send_text(writer, line)
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            await _log.unsubscribe(queue)
        writer.close()
        return

    # Unknown WS path
    await _send_http(writer, 404, b"not found")
    writer.close()


# ---------------------------------------------------------------------------
# Public server
# ---------------------------------------------------------------------------

async def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    install_log_broker()
    srv = await asyncio.start_server(_handle_connection, host=host, port=port)
    log.info("agent server listening on %s:%d (pid=%d)", host, port, os.getpid())
    try:
        await srv.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        srv.close()
        await srv.wait_closed()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(serve())
