"""FastAPI application entry point."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .core.storage import SESSIONS_DIR, STATIC_DIR, ensure_dirs
from .routes.calibration import router as calibration_router
from .routes.cameras import router as cameras_router
from .routes.profiles import router_chessboards, router_profiles
from .routes.remote_ssh import router as remote_ssh_router
from .routes.sessions import router as sessions_router
from .routes.stream import router as stream_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Sintez Camera Calibration", version="0.1.0")

ensure_dirs()

app.include_router(cameras_router)
# Both /profiles (legacy alias) and /chessboards (new canonical) are mounted
# so older clients keep working while new ones use the new prefix.
app.include_router(router_chessboards)
app.include_router(router_profiles)
app.include_router(sessions_router)
app.include_router(calibration_router)
app.include_router(stream_router)
app.include_router(remote_ssh_router)


@app.on_event("shutdown")
def _stop_remote_agents() -> None:
    """Auto-disable every remote agent when the server shuts down, so agents
    don't linger on remote boxes after the app exits."""
    from .core.remote_link import agent_registry
    agent_registry.disable_all()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")


# Serve static UI at /static/*
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR), html=True),
    name="static",
)

# Serve captured frames and result files directly
app.mount(
    "/session-data",
    StaticFiles(directory=str(SESSIONS_DIR)),
    name="session-data",
)