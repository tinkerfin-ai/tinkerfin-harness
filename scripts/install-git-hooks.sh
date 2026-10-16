#!/usr/bin/env sh

set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
git -C "$ROOT_DIR" config --local core.hooksPath .githooks
echo "Enabled repository Git hooks: $ROOT_DIR/.githooks"
