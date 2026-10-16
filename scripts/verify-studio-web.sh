#!/usr/bin/env sh

set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
MODE=web

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [--skip-browser]" >&2
  exit 2
elif [ "${1:-}" = "--skip-browser" ]; then
  MODE=web-no-browser
elif [ "${1:-}" != "" ]; then
  echo "Unknown argument: $1" >&2
  exit 2
fi
exec uv run --project "$ROOT_DIR" python "$ROOT_DIR/scripts/studio_checks.py" "$MODE"
