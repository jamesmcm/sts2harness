#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ $# -gt 1 ]]; then
  echo "usage: $0 [trial-directory]" >&2
  exit 2
fi

if [[ $# -eq 1 ]]; then
  trial_root="$1"
else
  trial_root="$(ls -td "$repo_root"/.trial-opencode-go-* 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${trial_root:-}" ]]; then
  echo "No .trial-opencode-go-* directory found." >&2
  exit 1
fi

trial_root="$(realpath "$trial_root")"
config="$trial_root/orchestrator.opencode-go.trial.json"

if [[ ! -f "$config" ]]; then
  echo "Missing orchestrator config: $config" >&2
  exit 1
fi

if pgrep -f "pi_orchestrator\\.orchestrator --config $config" >/dev/null; then
  echo "Orchestrator already appears to be running for: $config" >&2
  exit 1
fi

python - <<'PY' "$config"
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
config = json.loads(path.read_text(encoding="utf-8"))
config.pop("max_steps", None)
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

echo "Resuming OpenCode Go trial: $trial_root"
echo "Using config: $config"

monitor_pid=""
cleanup() {
  if [[ -n "$monitor_pid" ]]; then
    kill "$monitor_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "${TRIAL_MONITOR:-1}" != "0" ]]; then
  python -u "$repo_root/scripts/monitor_opencode_go_trial.py" "$trial_root" &
  monitor_pid="$!"
  echo "Live monitor PID: $monitor_pid (disable with TRIAL_MONITOR=0)"
fi

python -m pi_orchestrator.orchestrator --config "$config"
