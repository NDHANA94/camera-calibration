#!/usr/bin/env bash
# Run the test suite. With no arguments, runs unit + integration tests
# (the default for CI). Pass `--e2e --camera=/dev/video0` to include
# the camera-in-the-loop test that needs a physical V4L2 device.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

# ROS2 ships an out-of-date `launch_testing_ros_pytest_entrypoint` plugin
# whose hook signatures don't match current pytest. Disable plugin autoload
# so it never tries to import the broken module.
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

# If --e2e is in args, pass through everything. Otherwise default to
# "all tests, skip e2e" via -m "not e2e".
if [[ " $* " == *" --e2e "* ]]; then
    exec pytest tests/ "$@"
else
    exec pytest tests/ -m "not e2e" "$@"
fi