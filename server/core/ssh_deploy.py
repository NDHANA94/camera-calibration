"""SSH-based remote agent management -- three independent operations.

  check_agent_installed(host, port, username, password)
      SSH in, return {"installed": bool, "version": str|None}

  install_agent(host, port, username, password)
      SSH in, upload local agent source, pip-install.
      Async generator -- yields progress strings.
      Lines prefixed "ERROR:" are fatal; "DONE:" means success.

  start_agent(host, port, username, password, agent_port=8765)
      SSH in, launch the agent server on the remote.
      Returns (ssh_conn, agent_port) where ssh_conn is a live
      asyncssh.SSHClientConnection that the caller MUST keep open
      (use tunnel_to_agent() to open direct-tcpip channels to the
      agent's HTTP/WS server).  When the caller is done they should
      call ssh_conn.close() (synchronous in asyncssh) and optionally
      await ssh_conn.wait_closed().

  tunnel_to_agent(ssh_conn, agent_port)
      Open a direct-tcpip channel to 127.0.0.1:<agent_port> on the
      remote (where the agent listens).  Returns (reader, writer)
      that the caller speaks HTTP / WebSocket on.

  open_agent_http(ssh_conn, agent_port, request_bytes)
      Convenience: open a channel, send *request_bytes* (a raw HTTP
      request), read the full response, close.  Returns the response
      bytes.  For non-trivial uses prefer tunnel_to_agent() and pump
      bytes yourself.

  tail_agent_log(host, port, username, password)
      Async generator: SSH into the remote and tail /tmp/sintez_agent.log.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator, Tuple

import asyncssh

log = logging.getLogger(__name__)

AGENT_SRC = Path(__file__).parent.parent.parent / "agent"
REMOTE_VENV = "/tmp/sintez_venv"
DEFAULT_AGENT_PORT = 8765
AGENT_LOG_PATH = "/tmp/sintez_agent.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_agent_wheel() -> tuple[str, bytes]:
    """Build a wheel from the local agent source. Returns (filename, bytes).

    IMPORTANT: pass the *absolute* path.  When the path is relative (e.g.
    "agent"), pip interprets it as a PyPI package name and downloads a
    completely unrelated package -- we hit this bug when AGENT_SRC was
    passed as a relative path; the resulting wheel was 3 kB of metadata
    for some other project called "agent".
    """
    import subprocess, sys, tempfile
    agent_src_abs = str(AGENT_SRC.resolve())
    with tempfile.TemporaryDirectory() as tmp:
        base = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", tmp]
        # Prefer --no-build-isolation: it reuses the server venv's setuptools/
        # wheel instead of fetching build deps from the network, so it's fast
        # and works offline.  Fall back to an isolated build if that fails
        # (e.g. build deps somehow missing from the server env).
        res = subprocess.run(
            base + ["--no-build-isolation", agent_src_abs],
            capture_output=True, text=True, timeout=300,
        )
        if res.returncode != 0 or not list(Path(tmp).glob("*.whl")):
            res = subprocess.run(
                base + [agent_src_abs],
                capture_output=True, text=True, timeout=300,
            )
        if res.returncode != 0:
            raise RuntimeError(f"pip wheel failed: {(res.stderr or res.stdout)[:400]}")
        wheels = list(Path(tmp).glob("*.whl"))
        if not wheels:
            raise RuntimeError(f"No wheel produced: {res.stderr}")
        whl = wheels[0]
        # Sanity: the wheel must contain our new server.py module
        import zipfile
        with zipfile.ZipFile(whl) as z:
            names = z.namelist()
        if not any(n.endswith("sintez_cam_agent/server.py") for n in names):
            raise RuntimeError(
                f"Built wheel {whl.name} is missing sintez_cam_agent/server.py -- "
                "this usually means pip wheel resolved a different package. "
                "Check that the local agent directory has a pyproject.toml."
            )
        return whl.name, whl.read_bytes()


async def _connect(host: str, port: int, username: str, password: str):
    return await asyncssh.connect(
        host, port=port, username=username, password=password,
        known_hosts=None,
    )


def _out(res) -> str:
    return ((res.stdout or "") + (res.stderr or "")).strip()


def local_agent_version() -> str:
    """Version of the agent source bundled with this server (the version a
    fresh install/update would deploy).  Read from the package __init__ so we
    don't have to import the agent."""
    init = AGENT_SRC / "sintez_cam_agent" / "__init__.py"
    try:
        for line in init.read_text().splitlines():
            if line.strip().startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return "?"


# Shell snippet that prints "FOUND <version>" or "NOT_FOUND".  Tries to read
# the installed package version (works regardless of where the entry-point
# script lives); falls back to "FOUND ?" if the binary exists but the version
# can't be read.
_DETECT_CMD = (
    "V=$(python3 -c 'import sintez_cam_agent as a; "
    "print(getattr(a, \"__version__\", \"?\"))' 2>/dev/null); "
    "if [ -n \"$V\" ]; then echo \"FOUND $V\"; "
    "elif test -x $HOME/.local/bin/sintez-cam-agent "
    "|| which sintez-cam-agent >/dev/null 2>&1; then echo 'FOUND ?'; "
    "else echo NOT_FOUND; fi"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_agent_installed(
    host: str, port: int, username: str, password: str
) -> dict:
    """Return ``{"installed", "version", "latest", "needs_update"}``.

    ``version`` is what's on the remote (or None), ``latest`` is the version
    bundled with this server, ``needs_update`` is True when they differ (or
    the remote version can't be determined).  Raises on SSH failure.
    """
    latest = local_agent_version()
    async with await _connect(host, port, username, password) as conn:
        res = await conn.run(_DETECT_CMD, check=False)
        raw = (res.stdout or "").strip()
        log.info("check_agent_installed stdout: %r", raw)
        if not raw.startswith("FOUND"):
            return {"installed": False, "version": None,
                    "latest": latest, "needs_update": True}
        parts = raw.split(None, 1)
        version = parts[1].strip() if len(parts) > 1 and parts[1].strip() != "?" else None
        needs_update = (version is None) or (version != latest)
        return {"installed": True, "version": version,
                "latest": latest, "needs_update": needs_update}


async def install_agent(
    host: str, port: int, username: str, password: str
) -> AsyncIterator[str]:
    """Async generator: install the agent on the remote host via SSH."""
    async with await _connect(host, port, username, password) as conn:
        yield "Building agent wheel..."
        try:
            whl_name, whl_bytes = await asyncio.get_event_loop().run_in_executor(
                None, _build_agent_wheel
            )
        except Exception as exc:
            yield f"ERROR: Wheel build failed -- {exc}"
            return

        remote_whl = f"/tmp/{whl_name}"
        yield f"Uploading wheel ({len(whl_bytes) // 1024} kB) via SFTP..."
        try:
            async with conn.start_sftp_client() as sftp:
                async with sftp.open(remote_whl, "wb") as fh:
                    await fh.write(whl_bytes)
        except Exception as exc:
            yield f"ERROR: Upload failed -- {exc}"
            return

        yield "Installing wheel (pip) -- this may take a minute..."
        # --force-reinstall so re-uploading the SAME version number still
        # replaces an out-of-date binary (e.g. an old 0.1.0 WS-client agent
        # being overwritten by a new 0.1.0 HTTP-server agent).  --no-deps so
        # we don't drag in / rebuild opencv & friends, which are installed
        # on the remote already and can take many minutes to build on ARM.
        _flags = "--force-reinstall --no-deps"
        pip_cmds = [
            f"pip3 install {_flags} {remote_whl}",
            f"pip3 install {_flags} --break-system-packages {remote_whl}",
            f"pip3 install {_flags} --user {remote_whl}",
            f"python3 -m pip install {_flags} {remote_whl}",
            f"python3 -m pip install {_flags} --break-system-packages {remote_whl}",
            f"python3 -m pip install {_flags} --user {remote_whl}",
        ]
        installed = False
        last_out = ""
        for cmd in pip_cmds:
            res = await conn.run(cmd, check=False)
            last_out = _out(res)
            if res.returncode == 0:
                installed = True
                break
            yield f"  -> {cmd.split()[0]} failed (rc={res.returncode})"

        if not installed:
            yield f"ERROR: pip install failed -- {last_out[:400]}"
            return

        yield "DONE: Agent installed successfully."


async def uninstall_agent(
    host: str, port: int, username: str, password: str
) -> AsyncIterator[str]:
    """Async generator: remove the agent from the remote host via SSH.

    Stops any running agent, pip-uninstalls the package (trying the same
    interpreter variants install uses), and removes the leftover entry-point
    script and log.  Yields progress strings; "DONE:"/"ERROR:" terminate.
    """
    async with await _connect(host, port, username, password) as conn:
        yield "Stopping any running agent..."
        # Kill by the exact command we launch so we don't touch other procs.
        await conn.run(
            "pkill -f 'sintez_cam_agent.*--host' 2>/dev/null; "
            "pkill -f 'bin/sintez-cam-agent' 2>/dev/null; true",
            check=False,
        )

        yield "Uninstalling package (pip)..."
        uninstall_cmds = [
            "pip3 uninstall -y sintez-cam-agent",
            "pip3 uninstall -y --break-system-packages sintez-cam-agent",
            "python3 -m pip uninstall -y sintez-cam-agent",
            "python3 -m pip uninstall -y --break-system-packages sintez-cam-agent",
        ]
        removed = False
        last_out = ""
        for cmd in uninstall_cmds:
            res = await conn.run(cmd, check=False)
            last_out = _out(res)
            if res.returncode == 0 and "not installed" not in last_out.lower():
                removed = True
                break

        yield "Removing leftover files..."
        await conn.run(
            f"rm -f $HOME/.local/bin/sintez-cam-agent {REMOTE_VENV}/bin/sintez-cam-agent "
            f"{AGENT_LOG_PATH} /tmp/sintez_cam_agent-*.whl 2>/dev/null; true",
            check=False,
        )

        # Confirm it's actually gone.
        res = await conn.run(_DETECT_CMD, check=False)
        if (res.stdout or "").strip().startswith("FOUND"):
            yield (f"ERROR: Agent still detected after uninstall -- "
                   f"{last_out[:300]}")
            return

        yield "DONE: Agent removed."


async def start_agent(
    host: str,
    port: int,
    username: str,
    password: str,
    agent_port: int = DEFAULT_AGENT_PORT,
) -> tuple:
    """Open a long-lived SSH session to the remote and launch the agent
    HTTP/WS server on ``127.0.0.1:agent_port`` (default 8765).

    Returns ``(ssh_conn, process)`` -- the live
    ``asyncssh.SSHClientConnection`` and the ``SSHClientProcess`` running
    the agent.  The agent runs in the FOREGROUND of an SSH channel (not
    detached), so when this connection drops -- e.g. the calibration server
    stops -- the agent receives SIGHUP and exits.  This is what makes the
    remote agent auto-disable when the app goes down.  The caller keeps the
    connection open and opens channels via :func:`tunnel_to_agent`.

    Raises RuntimeError if the agent binary isn't installed or the
    launch failed.
    """
    conn = await _connect(host, port, username, password)
    try:
        # Locate the agent binary on the remote.
        res = await conn.run(
            f"if test -x {REMOTE_VENV}/bin/sintez-cam-agent; then "
            f"  echo {REMOTE_VENV}/bin/sintez-cam-agent; "
            f"elif test -x $HOME/.local/bin/sintez-cam-agent; then "
            f"  echo $HOME/.local/bin/sintez-cam-agent; "
            f"elif which sintez-cam-agent 2>/dev/null; then "
            f"  which sintez-cam-agent; "
            f"else echo NOT_FOUND; fi",
            check=False,
        )
        agent_bin = (res.stdout or "").strip()
        if not agent_bin or agent_bin == "NOT_FOUND":
            conn.close()
            await conn.wait_closed()
            raise RuntimeError(
                "sintez-cam-agent not found on remote -- install the agent first."
            )

        # Probe the agent's version to pick the right launch flags.
        # Pre-0.2.0 binaries used the old WS-client CLI (--server/--token)
        # and don't accept --port/--host; the new server-style agent does.
        # When we see the old binary, the only fix is to reinstall.
        ver_res = await conn.run(
            f"{agent_bin} --help 2>&1 | head -1 || true",
            check=False,
        )
        help_first = (ver_res.stdout or "").strip()
        # New agent's --help is empty (click with no command = usage line);
        # old agent's --help printed "Usage: sintez-cam-agent [OPTIONS]"
        # which contained "No such option" in the error path.  Heuristic:
        # if --help output mentions "--port" we have the new binary,
        # otherwise it's the old one.
        full_help = await conn.run(
            f"{agent_bin} --help 2>&1 || true",
            check=False,
        )
        full_help_text = (full_help.stdout or "") + (full_help.stderr or "")
        is_new_agent = "--port" in full_help_text and "--host" in full_help_text
        if not is_new_agent:
            conn.close()
            await conn.wait_closed()
            raise RuntimeError(
                "The sintez-cam-agent on the remote box is OUT OF DATE "
                "(version < 0.2.0, no HTTP server). "
                "Click 'Install Agent' to upload the new version, then "
                "click 'Enable Agent' again.  (Detected --help output: "
                + (full_help_text.strip()[:200] or "<empty>") + ")"
            )

        # Launch the agent in the FOREGROUND of an SSH session channel.
        # `exec` replaces the shell with the agent so the agent IS the
        # channel's process: when this connection drops, sshd sends SIGHUP
        # and the agent exits (auto-disable on app-down).  Output goes to the
        # log file, which the /agent/{id}/log endpoint tails separately.
        log_line = (
            f"[launcher] $(date -Iseconds) starting: {agent_bin} "
            f"--host 127.0.0.1 --port {agent_port} --verbose"
        )
        launch_cmd = (
            f"bash -lc 'LOG={AGENT_LOG_PATH}; : > \"$LOG\"; "
            f"echo \"{log_line}\" > \"$LOG\"; "
            f"exec {agent_bin} --host 127.0.0.1 --port {agent_port} --verbose "
            f">> \"$LOG\" 2>&1'"
        )
        process = await conn.create_process(launch_cmd)

        # Give it a moment to bind; if it exits immediately (e.g. the port is
        # already in use) surface the reason from the log rather than failing
        # silently later.
        await asyncio.sleep(1.5)
        if getattr(process, "exit_status", None) is not None:
            tail = await conn.run(
                f"tail -n 20 {AGENT_LOG_PATH} 2>/dev/null", check=False,
            )
            err = (tail.stdout or "").strip()[-400:]
            conn.close()
            await conn.wait_closed()
            raise RuntimeError(f"Agent exited immediately:\n{err}")

        return conn, process
    except BaseException:
        # If anything went wrong after the connection was opened, close it
        # so we don't leak a dangling SSH session.
        try:
            conn.close()
            await conn.wait_closed()
        except Exception:
            pass
        raise


async def tunnel_to_agent(ssh_conn, agent_port: int):
    """Open a direct-tcpip channel to ``127.0.0.1:agent_port`` on the remote.

    Returns ``(reader, writer, closer)`` where reader/writer are the
    ``asyncssh.SSHReader``/``asyncssh.SSHWriter`` for the channel (speak HTTP
    or WebSocket on them like a socket).  ``closer`` is an async function the
    caller should ``await`` when done to close the channel cleanly.

    Uses asyncssh's high-level :meth:`open_connection`, which returns a
    ready-to-use reader/writer pair -- the manual ``SSHTCPSession`` approach
    breaks across asyncssh versions (``SSHReader`` now requires a ``chan``).
    """
    reader, writer = await ssh_conn.open_connection("127.0.0.1", agent_port)

    async def closer():
        try:
            writer.close()
        except Exception:
            pass
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return reader, writer, closer


async def open_agent_http(ssh_conn, agent_port: int, request_bytes: bytes,
                          read_timeout: float = 5.0) -> bytes:
    """Open a channel, send a single HTTP request, read the full response,
    close.  For streaming use :func:`tunnel_to_agent` directly.
    """
    reader, writer, closer = await tunnel_to_agent(ssh_conn, agent_port)
    try:
        writer.write(request_bytes)
        await writer.drain()
        # Read until the channel closes (Connection: close from agent).
        buf = b""
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=read_timeout)
                if not chunk:
                    break
                buf += chunk
        except asyncio.TimeoutError:
            pass
        return buf
    finally:
        try:
            writer.close()
        except Exception:
            pass
        await closer()


async def tail_agent_log(
    host: str,
    port: int,
    username: str,
    password: str,
    log_path: str = AGENT_LOG_PATH,
) -> AsyncIterator[str]:
    """Async generator: SSH into the remote and tail *log_path*."""
    async with await _connect(host, port, username, password) as conn:
        await conn.run(f"touch {log_path} 2>/dev/null || true", check=False)
        dump = await conn.run(f"cat {log_path} 2>/dev/null", check=False)
        if dump.stdout:
            for line in dump.stdout.splitlines():
                yield line
        yield "__READY__"
        process = await conn.create_process(f"tail -F -n 0 {log_path}")
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                yield line.rstrip("\n")
        finally:
            try:
                process.terminate()
            except Exception:
                pass
