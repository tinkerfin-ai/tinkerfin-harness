#!/usr/bin/env sh

set -eu

ROOT_DIR="$(git rev-parse --show-toplevel)"
cd "$ROOT_DIR"
git config --local core.hooksPath .githooks
echo "Enabled repository Git hooks: $ROOT_DIR/.githooks"
