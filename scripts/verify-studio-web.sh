#!/usr/bin/env sh

set -eu

ROOT_DIR=$(git rev-parse --show-toplevel)
MODE=web

if [ "${1:-}" = "--skip-browser" ]; then
  MODE=web-no-browser
elif [ "${1:-}" != "" ]; then
  echo "Unknown argument: $1" >&2
  exit 2
fi
exec uv run python "$ROOT_DIR/scripts/studio_checks.py" "$MODE"
