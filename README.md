# Sintez Camera Calibration

A browser-based tool for OpenCV camera calibration. It runs a single FastAPI
server with a no-build, vanilla-JS UI and works with **local cameras**
(USB / V4L2, laptop webcam, MIPI/CSI, RTSP, Aravis GigE) and **remote cameras**
on another machine through a small streaming agent that the server installs and
manages over SSH.

It supports **mono and stereo** calibration, guided auto-capture, and two
analysis tools built on top of a finished calibration: **back-projection**
(pixel → 3D world ray) and **stereo depth prediction** (disparity → depth
heatmap).

---

## Features

### Calibration
- **Modes** (`server/models/schemas.py`):
  - `mono` — one camera, one frame.
  - `stereo_lr` — one camera delivering a side-by-side **L|R** frame.
  - `stereo_separate` — two cameras (left + right), paired per frame.
- **Guided live capture**: live preview with chessboard-corner overlay,
  sub-pixel refinement, blur/quality rejection, automatic capture when the board
  is stable, and pose-coverage guidance that tells you which views are still
  missing.
- **Chessboard profiles**: save/load board geometry (inner corners, square size,
  calibration flags) and reuse them across sessions.
- **Results** written per session: `result.npz`, `result.yaml`
  (OpenCV `FileStorage`) and `meta.json`. Stereo results include the relative
  pose `R`, `T`, baseline, and rectification matrices `R1/R2/P1/P2/Q`.

### Local cameras
- Auto-discovery of V4L2 devices, plus RTSP URLs and Aravis/GenICam industrial
  cameras (optional `aravis` extra).

### Remote cameras (SSH-automated)
- From the **Remote** tab you give SSH credentials; the server then:
  1. checks whether `sintez-cam-agent` is installed on the target,
  2. installs it over SSH if needed,
  3. launches it on `127.0.0.1:<port>` (default **8765**) on the remote box, and
  4. reaches it through a `direct-tcpip` SSH tunnel — **no open ports, no public
     exposure**.
- **Lifecycle / heartbeat**: liveness is a `browser → server → agent` chain.
  The server health-checks the agent every ~5 s; if the browser closes/reloads
  (a `pagehide` beacon plus a viewer-timeout on the server) the agent is stopped
  and the remote camera released. The agent also runs a **self-watchdog** that
  exits and frees the camera if it loses contact with the host (e.g. the server
  dies or SSH drops).

### Analysis tools (on a finished session)
- **Back-projection** — pick a frame (live camera *or* a saved calibration
  frame), draw a bounding box over an object, enter an extrinsic pose
  (rotation vector + translation, world → camera), and compute the **3D world
  ray** (origin + unit direction) through the box centre.
- **Depth prediction** (stereo only) — rectifies the L|R pair, runs `StereoSGBM`,
  reprojects to 3D via `Q`, and renders a **depth heatmap** with a colorbar
  legend (near → far in mm). Draw a box to read the median depth of that region.

Both tools share live **agent/camera detection**: they show the agent status,
the detected cameras, and fall back cleanly to saved calibration frames when no
live camera is available.

---

## Architecture

```
Browser (vanilla JS)
   │  HTTP + Server-Sent Events / WebSocket
   ▼
FastAPI server  (server/)
   ├── local cameras  ──────────► OpenCV VideoCapture (this machine)
   └── remote cameras ──SSH────►  sintez-cam-agent (agent/) on the target box
                                   (HTTP/WS over a direct-tcpip tunnel)
```

- **Server** (`server/`): FastAPI app, OpenCV calibration, per-session streaming
  pipeline, SSH agent management, and the static UI.
- **Agent** (`agent/`): a tiny dependency-light asyncio HTTP/WebSocket server
  (`opencv-python` + `numpy` + `websockets`), small enough to run on embedded
  targets (e.g. Jetson / Orin NX). It only listens on loopback and is reached
  through the server's SSH connection.

### Project layout

```
CameraCalibration/
├── server/
│   ├── app.py            FastAPI entry point (routers + static mounts)
│   ├── routes/           HTTP/SSE/WS endpoints
│   │   ├── cameras.py        camera discovery
│   │   ├── profiles.py       chessboard/profile CRUD
│   │   ├── sessions.py       session lifecycle
│   │   ├── stream.py         local camera streaming (WebSocket)
│   │   ├── calibration.py    run calibration, intrinsics YAML
│   │   ├── remote_ssh.py     SSH agent install/enable/heartbeat + frame proxy
│   │   └── backproject.py    back-projection ray + stereo depth + raw relay
│   ├── core/             OpenCV + plumbing (calibration, frame_pipeline,
│   │                     guidance, quality, backproject, ssh_deploy,
│   │                     remote_link, stream_processing, camera_manager, …)
│   ├── models/           Pydantic schemas
│   ├── static/           index.html · app.js · styles.css (no build step)
│   └── data/             profiles/ and sessions/ (gitignored)
├── agent/                pip-installable sintez-cam-agent
├── scripts/              setup.sh · run.sh · run-tests.sh
└── tests/                pytest suite
```

---

## Requirements

- Python ≥ 3.10
- For remote cameras: SSH access to the target machine (the server installs the
  agent there automatically).

Key dependencies are declared in `pyproject.toml`
(FastAPI, uvicorn, `opencv-contrib-python`, numpy, asyncssh, …) and
`agent/pyproject.toml`.

## Setup

```bash
bash scripts/setup.sh
```

Creates `.venv/` and installs both the server and the agent in editable mode.
Idempotent.

## Run the server

```bash
bash scripts/run.sh            # uvicorn server.app:app on 127.0.0.1:8000
```

Then open <http://127.0.0.1:8000>. Extra args pass through to uvicorn, e.g.
`bash scripts/run.sh --reload`.

## Remote cameras

The normal path is fully automated from the UI: open the **Remote** tab, enter
the SSH host / user / password (profiles can be saved), **Connect**, then
**Enable Agent**. The server installs and starts `sintez-cam-agent` on the
target and streams its frames back.

You can also run the agent by hand on the target (it binds loopback only):

```bash
pip install -e ./agent          # from this repo, or: pip install sintez-cam-agent
sintez-cam-agent --port 8765    # --list-cameras to enumerate, -v for debug logs
```

> After changing the agent source, rebuild and reinstall it on the target for
> the change to take effect (the installed agent runs from the package, not the
> working tree).

## Calibration workflow

1. Pick **Local** or **Remote**, choose the camera and calibration mode, and a
   chessboard profile (or create one).
2. **Start Capture** — move the board around; the app auto-captures stable,
   well-spread, in-focus views and shows coverage guidance.
3. When enough views are collected, it runs calibration and shows the result
   (RMS, reprojection error, intrinsics YAML; stereo also shows baseline +
   relative pose).
4. From a finished session's detail panel you can open **Test Back-project** and,
   for stereo sessions, **Depth prediction**.

## Storage

```
server/data/
├── profiles/<name>.json                  saved chessboard/calibration profiles
└── sessions/<session_id>/
    ├── session.json                       name, source, mode, camera(s), state
    ├── image_size.json                    per-eye image size (+ stereo flag)
    ├── frames/NNNN.png                     annotated captured frames
    ├── corners/ | corners_left/ corners_right/   detected corner arrays (.npy)
    ├── result.npz                          K, dist, (stereo: K1/D1/K2/D2/R/T/Q…)
    ├── result.yaml                         OpenCV FileStorage
    └── meta.json                           RMS, reprojection error, baseline…
```

SSH connection profiles are stored alongside the data dir in `ssh_profiles.json`.

## HTTP API (overview)

| Prefix          | Purpose |
|-----------------|---------|
| `/cameras`      | discover / probe local cameras |
| `/profiles`, `/chessboards` | chessboard profile CRUD |
| `/sessions`     | create / list / start / abort / delete sessions |
| `/ws/local/{session_id}` | local camera frame stream (WebSocket) |
| `/calibrate/{session_id}` | run calibration; `/intrinsics` returns YAML |
| `/remote/*`     | SSH agent install/enable/disable, status, cameras, log; frame proxy |
| `/back-project/{session_id}/compute` | pixel bbox → 3D world ray |
| `/back-project/{session_id}/depth`   | stereo depth map + heatmap |
| `/back-project/stream/{session_id}`  | raw video relay for the analysis tools |

Interactive docs are available at `/docs` while the server is running.

## Tests

```bash
bash scripts/run-tests.sh                       # unit + integration (skips e2e)
bash scripts/run-tests.sh --e2e --camera=/dev/video0   # include camera-in-the-loop
```

## Tech stack

FastAPI · uvicorn · OpenCV (`opencv-contrib-python`) · NumPy · asyncssh on the
server; a minimal asyncio HTTP/WebSocket agent (`opencv-python` + `websockets`);
a dependency-free vanilla-JS frontend served as static files.
