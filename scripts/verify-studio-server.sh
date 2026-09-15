#!/usr/bin/env sh

set -eu

ROOT_DIR=$(git rev-parse --show-toplevel)
exec uv run python "$ROOT_DIR/scripts/studio_checks.py" server
