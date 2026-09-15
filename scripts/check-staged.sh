#!/usr/bin/env sh

set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run --project "$ROOT_DIR" python "$ROOT_DIR/scripts/studio_checks.py" staged
