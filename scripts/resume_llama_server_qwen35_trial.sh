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
  trial_root="$(ls -td "$repo_root"/.trial-llama-server-qwen35-9b-subagent-* 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${trial_root:-}" ]]; then
  echo "No .trial-llama-server-qwen35-9b-subagent-* directory found." >&2
  exit 1
fi

trial_root="$(realpath "$trial_root")"
config="$trial_root/orchestrator.llama-server.trial.json"
pi_config="$trial_root/pi_agent.llama-server.trial.json"
harness_config="$trial_root/sts2harness.llama-server.trial.json"
sqlite_path="$trial_root/runs.sqlite"

if [[ ! -f "$config" ]]; then
  echo "Missing orchestrator config: $config" >&2
  exit 1
fi
if [[ ! -f "$pi_config" ]]; then
  echo "Missing Pi agent config: $pi_config" >&2
  exit 1
fi
if [[ ! -f "$harness_config" ]]; then
  echo "Missing harness config: $harness_config" >&2
  exit 1
fi
if [[ ! -f "$sqlite_path" ]]; then
  echo "Missing run trace SQLite DB for sub-agent restore: $sqlite_path" >&2
  exit 1
fi

if pgrep -f "pi_orchestrator\\.orchestrator --config $config" >/dev/null; then
  echo "Orchestrator already appears to be running for: $config" >&2
  exit 1
fi

python - <<'PY' "$config" "$pi_config" "$harness_config" "$trial_root"
import json
import pathlib
import sys

config_path, pi_path, harness_path, trial = (
    pathlib.Path(arg) for arg in sys.argv[1:5]
)

harness = json.loads(harness_path.read_text(encoding="utf-8"))
harness["logging"]["sqlite_path"] = str(trial / "runs.sqlite")
harness["agent"]["condition_name"] = "llama-server-subagent-memory-trial"
harness["agent"]["prompt_version"] = "2026-06-28-llama-server-subagent"
harness["logging"]["harness_version"] = "local-llama-server-subagent-trial"
harness_path.write_text(json.dumps(harness, indent=2) + "\n", encoding="utf-8")

pi = json.loads(pi_path.read_text(encoding="utf-8"))
pi["harness_config"] = str(harness_path)
pi_path.write_text(json.dumps(pi, indent=2) + "\n", encoding="utf-8")

config = json.loads(config_path.read_text(encoding="utf-8"))
config["rpc_server_command"] = [
    "python",
    "pi_agent/rpc_server.py",
    "--config",
    str(pi_path),
]
config["model"] = {
    **config.get("model", {}),
    "provider": "llama_server",
    "model": "qwen35-9b",
    "base_url": config.get("model", {}).get("base_url", "http://127.0.0.1:8080/v1"),
    "max_tokens": 20000,
    "context_window_tokens": 48000,
    "cache_prompt": True,
    "thinking_mode": "auto",
    "no_thinking_action_threshold": 2,
}
config.pop("max_steps", None)
config["agent_context"] = {
    **config.get("agent_context", {}),
    "enabled": True,
    "compaction_token_threshold": 24000,
    "prompt_cache": True,
}
config["decision_log"] = str(trial / "decisions.jsonl")
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY

echo "Resuming llama-server qwen35-9b sub-agent trial: $trial_root"
echo "Using config: $config"
echo "Sub-agent context will be restored from SQLite trace: $sqlite_path"

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
