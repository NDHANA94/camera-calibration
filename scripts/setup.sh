#!/usr/bin/env bash
# Create venv and install both server + agent packages in editable mode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip wheel setuptools

# Server (this dir's pyproject.toml)
pip install -e .

# Agent
pip install -e ./agent

echo "Setup complete. Activate with: source .venv/bin/activate"
echo "Run server with: bash scripts/run.sh"