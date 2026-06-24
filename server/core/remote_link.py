"""Remote-link: manages remote agent connections.

Two flows co-exist:

  Manual token flow  (old, for hand-run sintez-cam-agent):
      registry.issue_token(session_id) -> token
      Agent connects to /ws/remote/{session_id}?token=TOKEN

  SSH-automated flow (current, deployed by the app):

      The server keeps ONE persistent asyncssh connection to the remote
      box.  Whenever the laptop needs to talk to the agent, it opens a
      `direct-tcpip` channel through that connection to 127.0.0.1:8765
      on the remote (where the agent's HTTP/WS server is listening).
      All agent traffic travels *inside* the existing SSH connection --
      no firewall, no port forwarding, no public exposure.

      agent_registry.issue_token() -> agent_id
      The agent is started via SSH, listens on 127.0.0.1:8765, and the
      server proxies to it on demand.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import WebSocket

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Old manual-token registry  (/ws/remote/{session_id})
# ---------------------------------------------------------------------------

@dataclass
class RemoteAgent:
    websocket: WebSocket
    session_id: str
    ready: asyncio.Event = field(default_factory=asyncio.Event)


class RemoteLinkRegistry:
    def __init__(self) -> None:
        self._tokens: Dict[str, str] = {}
        self._agents: Dict[str, RemoteAgent] = {}

    def issue_token(self, session_id: str) -> str:
        token = secrets.token_urlsafe(24)
        self._tokens[token] = session_id
        return token

    def consume(self, token: str) -> Optional[str]:
        return self._tokens.pop(token, None)

    def register(self, session_id: str, ws: WebSocket) -> RemoteAgent:
        agent = RemoteAgent(websocket=ws, session_id=session_id)
        self._agents[session_id] = agent
        return agent

    def unregister(self, session_id: str) -> None:
        self._agents.pop(session_id, None)

    def get(self, session_id: str) -> Optional[RemoteAgent]:
        return self._agents.get(session_id)


registry = RemoteLinkRegistry()


# ---------------------------------------------------------------------------
# SSH-automated registry
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Live state for one SSH-deployed remote agent."""
    agent_id: str
    # The persistent SSH connection to the remote box.  All traffic to
    # the agent flows through this connection via direct-tcpip channels.
    ssh_conn: object  # asyncssh.SSHClientConnection (untyped to avoid hard dep)
    creds: dict
    # The SSHClientProcess running the agent in the foreground of a channel.
    # Killing it (or closing ssh_conn) stops the remote agent.
    process: object = None
    cameras: List[dict] = field(default_factory=list)
    cameras_ready: asyncio.Event = field(default_factory=asyncio.Event)
    # Set by bind_session() once user picks a camera and starts calibration
    start_event: asyncio.Event = field(default_factory=asyncio.Event)
    start_camera_id: Optional[str] = None
    # Second camera id (STEREO_SEPARATE only).  When set, the remote stream
    # uses the agent's /ws/stream_stereo endpoint.
    start_camera_id_2: Optional[str] = None
    session_id: Optional[str] = None
    # Browser WebSocket watching the remote feed (populated by /ws/watch)
    viewer_ws: Optional[WebSocket] = None
    # Port the agent is listening on (default 8765)
    agent_port: int = 8765
    # Heartbeat: monotonic time of the last successful agent health check, and
    # the background task that runs the heartbeat loop.
    last_heartbeat: float = 0.0
    heartbeat_task: object = None


class RemoteAgentRegistry:
    # How long an "issued but not yet connected" agent_id is remembered.
    # During this window the polling endpoint returns 200 (connected=false)
    # instead of 404, so the UI doesn't get spurious 404s while the SSH-launched
    # agent process is spinning up.
    PENDING_TTL_S = 120.0

    def __init__(self) -> None:
        self._agents: Dict[str, AgentState] = {}   # agent_id -> state
        self._by_session: Dict[str, str] = {}      # session_id -> agent_id
        # agent_id -> monotonic timestamp of issue.  Used so polling returns
        # 200 with connected=false while we wait for the agent to register,
        # and so we don't 404 a brand-new id during the connect window.
        self._issued: Dict[str, float] = {}
        # agent_id -> SSH creds.  Used by the /agent/{id}/log SSE endpoint
        # to tail the remote agent log without the browser having to
        # re-send credentials.  Cleared when the agent disconnects or
        # after PENDING_TTL_S.
        self._creds: Dict[str, dict] = {}
        # agent_id -> {"reason", "ts"} for agents that went DOWN unexpectedly
        # (heartbeat lost), so the status endpoint can tell the UI the agent
        # died vs. was never started.  Reaped after DOWN_TTL_S.
        self._down: Dict[str, dict] = {}

    DOWN_TTL_S = 300.0

    # -- Token lifecycle --

    def issue_token(
        self,
        host: str = "",
        port: int = 22,
        username: str = "",
        password: str = "",
    ) -> str:
        """Return a new agent_id. Stash the SSH creds for later log-tailing."""
        agent_id = secrets.token_hex(8)
        self._issued[agent_id] = asyncio.get_event_loop().time()
        if host:
            self._creds[agent_id] = {
                "host": host, "port": int(port), "username": username, "password": password,
            }
        return agent_id

    def get_creds(self, agent_id: str) -> Optional[dict]:
        return self._creds.get(agent_id)

    def _gc_pending(self) -> None:
        """Drop expired pending entries so the dict doesn't grow forever."""
        now = asyncio.get_event_loop().time()
        stale = [aid for aid, ts in self._issued.items()
                 if aid not in self._agents and (now - ts) > self.PENDING_TTL_S]
        for aid in stale:
            self._issued.pop(aid, None)
            self._creds.pop(aid, None)

    # -- Agent lifecycle --

    def register(self, agent_id: str, ssh_conn, creds: dict,
                 agent_port: int = 8765, process=None) -> AgentState:
        state = AgentState(
            agent_id=agent_id, ssh_conn=ssh_conn, creds=creds,
            agent_port=agent_port, process=process,
        )
        self._agents[agent_id] = state
        self._down.pop(agent_id, None)
        return state

    def unregister(self, agent_id: str) -> None:
        state = self._agents.pop(agent_id, None)
        if state:
            if state.session_id:
                self._by_session.pop(state.session_id, None)
            # Stop the heartbeat loop for this agent.
            try:
                if state.heartbeat_task is not None:
                    state.heartbeat_task.cancel()
            except Exception:
                pass
            # Ask the remote agent process to stop promptly.  Closing the SSH
            # connection below would also kill it via SIGHUP, but terminate()
            # is a clean, immediate signal.
            try:
                if state.process is not None:
                    state.process.terminate()
            except Exception:
                pass
            # Close the persistent SSH connection.  Wrap in a Task so
            # callers that aren't already in an async context don't have
            # to await us; the connection's __del__ would also close it.
            try:
                if state.ssh_conn is not None and not state.ssh_conn.is_closing():
                    # asyncssh's close() is synchronous; wait_closed() is the
                    # coroutine.  Closing is enough here -- schedule a
                    # wait_closed() when a loop is running so the connection
                    # tears down cleanly, but never await close() (it returns
                    # None, which would raise "NoneType can't be awaited").
                    state.ssh_conn.close()
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            loop.create_task(state.ssh_conn.wait_closed())
                    except Exception:
                        pass
            except Exception:
                pass
        # Keep creds around for a bit so the log-tail endpoint can still
        # reach the remote after the agent exits.  _gc_pending() reaps them.

    def get(self, agent_id: str) -> Optional[AgentState]:
        """Return the AgentState for *agent_id*, or None if it never existed
        (or the pending entry has expired)."""
        self._gc_pending()
        return self._agents.get(agent_id)

    def mark_down(self, agent_id: str, reason: str) -> None:
        """Record that an agent went down unexpectedly (heartbeat lost) and
        tear it down.  The status endpoint reports this so the UI can flip the
        agent back to 'not running' and let the user re-enable."""
        if agent_id in self._agents or agent_id in self._issued:
            self._down[agent_id] = {
                "reason": reason, "ts": asyncio.get_event_loop().time(),
            }
        self._issued.pop(agent_id, None)
        self.unregister(agent_id)

    def down_info(self, agent_id: str) -> Optional[dict]:
        """Return {'reason', 'ts'} if this agent went down recently, else None."""
        now = asyncio.get_event_loop().time()
        stale = [aid for aid, d in self._down.items()
                 if (now - d["ts"]) > self.DOWN_TTL_S]
        for aid in stale:
            self._down.pop(aid, None)
        return self._down.get(agent_id)

    def is_issued(self, agent_id: str) -> bool:
        """True iff this agent_id was issued (and the pending entry hasn't
        expired). Lets callers distinguish "agent never started" from
        "agent started but not yet connected"."""
        self._gc_pending()
        return agent_id in self._issued

    def get_by_session(self, session_id: str) -> Optional[AgentState]:
        aid = self._by_session.get(session_id)
        return self._agents.get(aid) if aid else None

    # -- Session binding (fires streaming) --

    def bind_session(
        self,
        agent_id: str,
        session_id: str,
        camera_id: str,
        camera_id_2: Optional[str] = None,
    ) -> None:
        """Associate agent with a session and trigger camera streaming.

        For STEREO_SEPARATE pass a second camera id; the remote stream will
        then open /ws/stream_stereo instead of /ws/stream.
        """
        state = self._agents.get(agent_id)
        if state is None:
            raise KeyError(f"No agent with id {agent_id!r}")
        state.session_id = session_id
        state.start_camera_id = camera_id
        state.start_camera_id_2 = camera_id_2
        self._by_session[session_id] = agent_id
        state.start_event.set()

    # -- Viewer (browser watching the remote feed) --

    def set_viewer(self, session_id: str, ws: Optional[WebSocket]) -> None:
        state = self.get_by_session(session_id)
        if state:
            state.viewer_ws = ws

    # -- Bulk shutdown (auto-disable every agent when the app goes down) --

    def disable_all(self) -> None:
        """Stop and unregister every running agent.  Called on server
        shutdown so remote agents don't linger after the app exits."""
        for agent_id in list(self._agents.keys()):
            self.unregister(agent_id)


agent_registry = RemoteAgentRegistry()
