# Camera Calibration Web App

A browser-based interface for OpenCV camera calibration, supporting both local cameras (USB/V4L2, laptop webcam, MIPI/CSI, RTSP, Aravis) and remote cameras (via a small Python agent installed on the target machine).

## Features

- **Local mode**: discover cameras on this machine, configure chessboard parameters, live preview with corner overlay, auto-capture when stable, blur rejection, save `.npz` + `.yaml` + `meta.json`.
- **Remote mode**: same workflow against a camera on another machine. The remote machine runs `sintez-cam-agent` (a small pip-installable Python package) which streams WebSocket frames back to this server.
- **Profiles**: savable/loadable calibration parameter sets (board size, square size, calibration flags).
- **No build step**: vanilla JS frontend served as static files.

## Layout

```
CameraCalibration/
├── server/        FastAPI app (the calibration server + UI)
├── agent/         pip-installable remote agent (run on the target machine)
└── scripts/       setup.sh, run.sh
```

## Setup

```bash
bash scripts/setup.sh
```

This creates `.venv/`, installs both packages in editable mode, and is idempotent.

## Run the server

```bash
bash scripts/run.sh
```

Open <http://127.0.0.1:8000>.

## Run the remote agent on another machine

```bash
pip install -e ./agent            # from the cloned repo, OR
pip install sintez-cam-agent      # once published

sintez-cam-agent \
    --server ws://<server-host>:8000 \
    --token <one-time-token-from-server> \
    --session-id <session-id>
```

## Storage

- `server/data/profiles/*.json` — saved calibration profiles
- `server/data/sessions/<id>/result.npz` + `result.yaml` + `meta.json` — calibration results

## Architecture

See [/memories/session/plan-architecture.md](memories/session/plan-architecture.md) for the original plan.