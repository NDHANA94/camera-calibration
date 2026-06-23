"""SSH-based remote calibration management.

SSH Profiles (saved connections):
    GET    /remote/ssh-profiles            -- list saved profiles
    POST   /remote/ssh-profiles            -- save / upsert a profile
    DELETE /remote/ssh-profiles/{name}     -- delete a profile

Remote agent lifecycle:
    POST   /remote/ssh-check               -- SSH in, check if agent installed
    POST   /remote/ssh-install             -- install agent via SSH (SSE stream)
    POST   /remote/ssh-enable              -- open SSH session, launch agent,
                                              keep the SSH connection alive
    GET    /remote/agent/{agent_id}/cameras
                                            -- poll camera list (proxied through
                                              the SSH tunnel as a fresh HTTP
                                              request each call)
    POST   /remote/agent/{agent_id}/bind   -- bind agent to session+camera
    GET    /remote/agent/{agent_id}/log    -- SSE tail of /tmp/sintez_agent.log

Frame stream:
    GET    /remote/stream/{session_id}     -- SSE proxy of agent's binary
                                              JPEG stream.  Browser opens
                                              this instead of /ws/watch.

The agent is a tiny HTTP/WS server on the remote box; the calibration
server reaches it via direct-tcpip channels through the persistent
SSH connection opened in /remote/ssh-enable.  No firewall, no port
forwarding, no public exposure.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import socket
import struct
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..core.remote_link import agent_registry
from ..core.storage import SESSIONS_DIR
from ..core.ssh_deploy import DEFAULT_AGENT_PORT, start_agent, tunnel_to_agent

router = APIRouter(prefix="/remote", tags=["remote-ssh"])
log = logging.getLogger(__name__)

PROFILES_FILE = SESSIONS_DIR.parent / "ssh_profiles.json"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SshProfile(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str
    password: str = ""
    agent_port: int = DEFAULT_AGENT_PORT


class SshCheckPayload(BaseModel):
    host: str
    port: int = 22
    username: str
    password: str


class SshInstallPayload(SshCheckPayload):
    pass


class SshEnablePayload(SshCheckPayload):
    server_url: str = ""
    agent_port: int = DEFAULT_AGENT_PORT


class BindPayload(BaseModel):
    session_id: str
    camera_id: str


class AgentIdPayload(BaseModel):
    agent_id: str


# ---------------------------------------------------------------------------
# SSH Profile helpers
# ---------------------------------------------------------------------------

def _load_profiles() -> List[dict]:
    if not PROFILES_FILE.exists():
        return []
    try:
        return json.loads(PROFILES_FILE.read_text())
    except Exception:
        return []


def _save_profiles(profiles: List[dict]) -> None:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2))


@router.get("/ssh-profiles", response_model=List[SshProfile])
def list_profiles() -> List[SshProfile]:
    return [SshProfile(**p) for p in _load_profiles()]


@router.post("/ssh-profiles", response_model=SshProfile, status_code=200)
def save_profile(payload: SshProfile) -> SshProfile:
    profiles = _load_profiles()
    profiles = [p for p in profiles if p.get("name") != payload.name]
    profiles.append(payload.model_dump())
    _save_profiles(profiles)
    return payload


@router.delete("/ssh-profiles/{name}", status_code=204)
def delete_profile(name: str) -> None:
    profiles = _load_profiles()
    new = [p for p in profiles if p.get("name") != name]
    if len(new) == len(profiles):
        raise HTTPException(404, f"Profile {name!r} not found")
    _save_profiles(new)


# ---------------------------------------------------------------------------
# Remote agent lifecycle
# ---------------------------------------------------------------------------

@router.post("/ssh-check")
async def ssh_check(payload: SshCheckPayload) -> dict:
    from ..core.ssh_deploy import check_agent_installed
    try:
        return await check_agent_installed(
            payload.host, payload.port, payload.username, payload.password
        )
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.post("/ssh-install")
async def ssh_install(payload: SshInstallPayload) -> StreamingResponse:
    from ..core.ssh_deploy import install_agent

    async def sse():
        try:
            async for line in install_agent(
                payload.host, payload.port, payload.username, payload.password
            ):
                yield f"data: {json.dumps({'message': line})}\n\n"
                await asyncio.sleep(0)
        except Exception as exc:
            log.exception("ssh-install error")
            yield f"data: {json.dumps({'message': f'ERROR: {exc}'})}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@router.post("/ssh-uninstall")
async def ssh_uninstall(payload: SshInstallPayload) -> StreamingResponse:
    """Remove the agent from the remote host (SSE progress stream)."""
    from ..core.ssh_deploy import uninstall_agent

    async def sse():
        try:
            async for line in uninstall_agent(
                payload.host, payload.port, payload.username, payload.password
            ):
                yield f"data: {json.dumps({'message': line})}\n\n"
                await asyncio.sleep(0)
        except Exception as exc:
            log.exception("ssh-uninstall error")
            yield f"data: {json.dumps({'message': f'ERROR: {exc}'})}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


@router.post("/ssh-enable")
async def ssh_enable(payload: SshEnablePayload, request: Request) -> dict:
    """Open a persistent SSH session, launch the agent, return agent_id."""
    if payload.server_url:
        server_base = payload.server_url.rstrip("/")
    else:
        host_hdr = request.headers.get("host", "127.0.0.1:8000")
        scheme = "https" if request.url.scheme == "https" else "http"
        try:
            server_port = request.url.port or 8000
        except Exception:
            server_port = 8000
        hostname = host_hdr.split(":", 1)[0] if ":" in host_hdr else host_hdr
        if hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("8.8.8.8", 80))
                    lan_ip = s.getsockname()[0]
            except Exception:
                lan_ip = None
            server_base = (
                f"{scheme}://{lan_ip}:{server_port}" if lan_ip
                else f"{scheme}://{hostname}:{server_port}"
            )
        else:
            server_base = f"{scheme}://{hostname}:{server_port}"

    # Refuse loopback URLs to a remote SSH target -- it would fail to connect.
    parsed = server_base.split("://", 1)[-1]
    host_part = parsed.split(":", 1)[0] if ":" in parsed else parsed
    if host_part in ("127.0.0.1", "localhost") and payload.host not in (
        "127.0.0.1", "localhost", "::1",
    ):
        raise HTTPException(
            400,
            "Server URL resolves to loopback, but the SSH target is a "
            "remote host. Set 'Server URL (as seen from remote)' to this "
            "machine's LAN IP (e.g. http://192.168.1.10:8000).",
        )

    creds = {
        "host": payload.host, "port": int(payload.port),
        "username": payload.username, "password": payload.password,
    }
    agent_id = agent_registry.issue_token(**creds)

    try:
        ssh_conn, process = await start_agent(
            payload.host, payload.port, payload.username, payload.password,
            agent_port=payload.agent_port,
        )
    except Exception as exc:
        raise HTTPException(502, str(exc))

    state = agent_registry.register(
        agent_id, ssh_conn, creds, agent_port=payload.agent_port, process=process,
    )

    # Verify reachability in the background: ask the agent for its cameras so
    # we know the tunnel is alive.  The polling endpoint returns them once
    # they show up.
    asyncio.create_task(_probe_cameras(agent_id, state))

    return {"agent_id": agent_id, "server_url": server_base}


async def _probe_cameras(agent_id: str, state) -> bool:
    """Open a fresh tunnel, ask the agent for /cameras, store the result on
    *state*.  Returns True on success.  Used both on enable and on demand
    (camera refresh)."""
    try:
        reader, writer, closer = await tunnel_to_agent(state.ssh_conn, state.agent_port)
        try:
            writer.write(
                b"GET /cameras HTTP/1.1\r\nHost: x\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            buf = b""
            while True:
                # The agent enumerates cameras (subprocess probes, ~4s) before
                # sending anything, so allow generous headroom on the read.
                chunk = await asyncio.wait_for(reader.read(4096), timeout=15)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            cameras = json.loads(body).get("cameras", [])
            state.cameras = cameras
            state.cameras_ready.set()
            log.info("agent %s reported %d cameras", agent_id, len(cameras))
            return True
        finally:
            try: writer.close()
            except Exception: pass
            await closer()
    except Exception as exc:
        log.warning("agent %s probe failed: %s", agent_id, exc)
        return False


@router.get("/agent/{agent_id}/cameras")
async def get_cameras(agent_id: str, refresh: bool = False) -> dict:
    """Return the camera list reported by the agent.

    While the agent is spinning up we return 200 with connected=false
    so the UI doesn't see a misleading 404.  Only ids we never issued
    return 404.  ``?refresh=1`` re-probes the agent (re-enumerates the
    remote /dev/video* devices) before returning.
    """
    state = agent_registry.get(agent_id)
    if state is None:
        if agent_registry.is_issued(agent_id):
            return {"connected": False, "cameras": [], "pending": True}
        raise HTTPException(404, "Agent not found or already disconnected")
    if refresh:
        await _probe_cameras(agent_id, state)
    return {
        "connected": state.cameras_ready.is_set(),
        "cameras": state.cameras,
    }


@router.post("/ssh-disable")
async def ssh_disable(payload: AgentIdPayload) -> dict:
    """Disable (stop) a running remote agent: kill the agent process and
    close the SSH connection.  Idempotent -- returns ok even if the agent
    is already gone."""
    agent_registry.unregister(payload.agent_id)
    return {"ok": True, "agent_id": payload.agent_id}


@router.post("/agent/{agent_id}/bind")
async def bind_agent(agent_id: str, payload: BindPayload) -> dict:
    try:
        agent_registry.bind_session(agent_id, payload.session_id, payload.camera_id)
    except KeyError:
        raise HTTPException(404, "Agent not found or already disconnected")
    return {"ok": True, "session_id": payload.session_id, "camera_id": payload.camera_id}


@router.get("/agent/{agent_id}/log")
async def tail_agent_log(agent_id: str) -> StreamingResponse:
    """SSE tail of /tmp/sintez_agent.log on the remote box."""
    from ..core.ssh_deploy import tail_agent_log
    creds = agent_registry.get_creds(agent_id)
    if creds is None:
        raise HTTPException(404, "Agent not found or log no longer available")

    async def sse():
        try:
            async for line in tail_agent_log(
                creds["host"], creds["port"], creds["username"], creds["password"],
            ):
                yield f"data: {json.dumps({'line': line})}\n\n"
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Agent log tail error: %s", exc)
            yield f"data: {json.dumps({'line': f'ERROR: {exc}'})}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Frame stream proxy -- opens a fresh SSH channel and bridges frames to
# the browser via Server-Sent Events.
# ---------------------------------------------------------------------------

@router.get("/stream/{session_id}")
async def remote_stream(session_id: str) -> StreamingResponse:
    """SSE proxy of the remote agent's binary JPEG stream.

    The browser opens ``EventSource('/remote/stream/<sid>')`` and gets
    base64-encoded JPEG frames as ``data: {"frame": "<b64>"}\\n\\n``
    messages, plus a ``data: {"type": "status", ...}`` marker at start.
    """
    state = agent_registry.get_by_session(session_id)
    if state is None or not state.start_camera_id:
        raise HTTPException(404, "No remote stream for this session")
    camera_id = state.start_camera_id
    ssh_conn = state.ssh_conn
    agent_port = state.agent_port

    async def sse():
        # Open a fresh channel to the agent for this stream
        try:
            reader, writer, closer = await tunnel_to_agent(ssh_conn, agent_port)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            return
        try:
            # Send HTTP/1.1 upgrade to the agent's /ws/stream.  The agent
            # implements a minimal WebSocket server (RFC 6455) so we
            # speak that protocol over the channel.
            key = base64.b64encode(hashlib.md5(str(id(reader)).encode()).digest()).decode()
            req = (
                f"GET /ws/stream?camera={camera_id}&fps=15&quality=80 HTTP/1.1\r\n"
                f"Host: x\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
            writer.write(req)
            await writer.drain()

            # Read until we see \r\n\r\n (end of HTTP response headers).
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
                if not chunk:
                    raise ConnectionError("agent closed before sending upgrade response")
                head += chunk
            status_line = head.split(b"\r\n", 1)[0]
            if b"101" not in status_line:
                body = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
                yield f"data: {json.dumps({'type': 'error', 'message': 'upgrade failed: ' + status_line.decode(errors='replace') + ' ' + body[:200].decode(errors='replace')})}\n\n"
                return

            # Stream WebSocket frames: first text frame = status, then
            # binary frames = JPEGs.  We parse client-side and re-emit
            # each JPEG as a base64 SSE message.
            yield f"data: {json.dumps({'type': 'started', 'camera': camera_id})}\n\n"
            buf = head.split(b"\r\n\r\n", 1)[1]
            while True:
                while len(buf) < 2:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    buf += chunk
                b1, b2 = buf[0], buf[1]
                opcode = b1 & 0x0F
                n = b2 & 0x7F
                idx = 2
                if n == 126:
                    while len(buf) < idx + 2:
                        buf += await reader.read(4096)
                    n = struct.unpack(">H", buf[idx:idx+2])[0]
                    idx += 2
                elif n == 127:
                    while len(buf) < idx + 8:
                        buf += await reader.read(4096)
                    n = struct.unpack(">Q", buf[idx:idx+8])[0]
                    idx += 8
                # No client->server masking since we sent unmasked, but be safe
                masked = b2 & 0x80
                if masked:
                    while len(buf) < idx + 4:
                        buf += await reader.read(4096)
                    mask = buf[idx:idx+4]
                    idx += 4
                else:
                    mask = b""
                while len(buf) < idx + n:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    buf += chunk
                payload = bytes(buf[idx:idx+n])
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                buf = buf[idx+n:]

                if opcode == 0x8:  # close
                    return
                if opcode == 0x9:  # ping
                    continue
                if opcode == 0x1:  # text
                    try:
                        msg = json.loads(payload)
                        yield f"data: {json.dumps({'type': 'status', **msg})}\n\n"
                    except Exception:
                        pass
                elif opcode == 0x2:  # binary
                    yield f"data: {json.dumps({'type': 'frame', 'data': base64.b64encode(payload).decode()})}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("remote stream error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            try: writer.close()
            except Exception: pass
            try: await closer()
            except Exception: pass

    return StreamingResponse(sse(), media_type="text/event-stream")
