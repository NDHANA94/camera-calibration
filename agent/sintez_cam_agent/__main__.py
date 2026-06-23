"""CLI entry point for the remote camera streaming agent.

The agent runs a small HTTP/WebSocket server on 127.0.0.1:<port>.  The
calibration server reaches it by opening a `direct-tcpip` SSH channel to
that port — no firewall, no port forwarding, no public exposure.

Modes:

  Server-managed (recommended):
      sintez-cam-agent --port 8765
      The server starts the agent via SSH with this flag.

  Manual (hand-launched):
      sintez-cam-agent --port 8765
      The user is responsible for starting the agent on the remote box,
      and for providing an SSH-tunneled path to it (see docs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

import click

from .camera import list_local_cameras
from .server import serve

log = logging.getLogger(__name__)


@click.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Address to bind the agent server on (default loopback only).")
@click.option("--port", default=8765, show_default=True, type=int,
              help="Port to bind the agent server on.")
@click.option("--list-cameras", is_flag=True, help="Print available cameras and exit")
@click.option("--verbose", "-v", is_flag=True)
def main(
    host: str,
    port: int,
    list_cameras: bool,
    verbose: bool,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if list_cameras:
        click.echo(json.dumps(list_local_cameras(), indent=2))
        return

    log.info("starting sintez-cam-agent on %s:%d", host, port)
    try:
        asyncio.run(serve(host=host, port=port))
    except KeyboardInterrupt:
        click.echo("Interrupted", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
