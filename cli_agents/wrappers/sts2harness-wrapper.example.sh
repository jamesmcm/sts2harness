#!/usr/bin/env bash
set -euo pipefail

HARNESS_ROOT="${HARNESS_ROOT:-/opt/sts2harness}"
HARNESS_CONFIG="${HARNESS_CONFIG:-/var/lib/sts2harness/official/sts2harness.json}"

cd "$HARNESS_ROOT"
exec uv run python main.py --config "$HARNESS_CONFIG" "$@"
