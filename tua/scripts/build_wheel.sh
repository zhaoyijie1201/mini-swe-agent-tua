#!/usr/bin/env bash
# Build the wheel of this fork that MiniSweAgentFork uploads into the task containers.
set -euo pipefail
cd "$(dirname "$0")/../.."
rm -rf tua/dist
python -m pip wheel --no-deps --no-build-isolation -q -w tua/dist . 2>/dev/null || python -m pip wheel --no-deps -q -w tua/dist .
ls -1 tua/dist/*.whl
