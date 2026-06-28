#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

timestamp="$(date +%Y%m%d-%H%M%S)"
trial_root="${TRIAL_ROOT:-$repo_root/.trial-llama-server-qwen35-9b-subagent-$timestamp}"
memory_repo="$trial_root/agent-memory-source"
memory_worktree="$trial_root/agent-memory-worktree"
memory_branch="${MEMORY_BRANCH:-memory/llama-server-qwen35-9b-subagent-trial}"

if [[ "${LLAMA_SERVER_PREFLIGHT:-1}" != "0" ]]; then
  python - <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.request

config = json.loads(
    pathlib.Path("pi_orchestrator/config/orchestrator.llama-server.local.json")
    .read_text(encoding="utf-8")
)
base_url = config["model"].get("base_url", "http://127.0.0.1:8080/v1").rstrip("/")
try:
    with urllib.request.urlopen(f"{base_url}/models", timeout=5) as response:
        response.read()
except (OSError, urllib.error.URLError) as exc:
    print(
        f"llama-server preflight failed for {base_url}/models: {exc}",
        file=sys.stderr,
    )
    print(
        "Start llama-server first, or set LLAMA_SERVER_PREFLIGHT=0 to skip this check.",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
fi

mkdir -p "$trial_root"

if [[ -e "$memory_worktree" ]]; then
  echo "Memory worktree already exists: $memory_worktree" >&2
  exit 1
fi

mkdir -p "$memory_repo"
git -C "$memory_repo" init >/dev/null
git -C "$memory_repo" \
  -c user.name=sts2harness \
  -c user.email=sts2harness@example.invalid \
  commit --allow-empty -m "initial memory repo" >/dev/null
git -C "$memory_repo" worktree add -B "$memory_branch" "$memory_worktree" >/dev/null

for path in STRATEGY.md CURRENT_RUN.md BATTLE_LOG.md HARNESS_BUGS.md; do
  if [[ -f "$repo_root/pi_agent/memory/$path" ]]; then
    cp "$repo_root/pi_agent/memory/$path" "$memory_worktree/$path"
  else
    : > "$memory_worktree/$path"
  fi
done

python - <<'PY' "$trial_root" "$memory_repo" "$memory_worktree" "$memory_branch"
import json
import pathlib
import sys

trial, repo, worktree = map(pathlib.Path, sys.argv[1:4])
branch = sys.argv[4]

harness = json.loads(pathlib.Path("sts2harness.llama-server.json").read_text())
harness["agent"]["condition_name"] = "llama-server-subagent-memory-trial"
harness["agent"]["prompt_version"] = "2026-06-28-llama-server-subagent"
harness["logging"]["harness_version"] = "local-llama-server-subagent-trial"
seed_file = harness.get("run_setup", {}).get("seed_file")
if seed_file:
    seed_path = pathlib.Path(seed_file).expanduser()
    if not seed_path.is_absolute():
        seed_path = pathlib.Path.cwd() / seed_path
    harness["run_setup"]["seed_file"] = str(seed_path)
harness["run_setup"]["progress_file"] = str(trial / "progress.json")
harness["logging"]["sqlite_path"] = str(trial / "runs.sqlite")
harness["logging"].update(
    {
        "memory_git_dir": str(worktree),
        "memory_git_source": str(repo),
        "memory_git_branch": branch,
        "memory_git_init": True,
        "memory_commit_on_run_start": True,
        "memory_commit_on_room_change": True,
        "memory_commit_on_run_end": True,
        "memory_commit_paths": [
            "STRATEGY.md",
            "CURRENT_RUN.md",
            "BATTLE_LOG.md",
            "HARNESS_BUGS.md",
        ],
    }
)
(trial / "sts2harness.llama-server.trial.json").write_text(
    json.dumps(harness, indent=2) + "\n",
    encoding="utf-8",
)

pi = json.loads(
    pathlib.Path("pi_agent/config/pi_agent.llama-server.local.json").read_text()
)
pi["harness_config"] = str(trial / "sts2harness.llama-server.trial.json")
pi["memory_root"] = str(worktree)
(trial / "pi_agent.llama-server.trial.json").write_text(
    json.dumps(pi, indent=2) + "\n",
    encoding="utf-8",
)

orch = json.loads(
    pathlib.Path("pi_orchestrator/config/orchestrator.llama-server.local.json").read_text()
)
orch["rpc_server_command"] = [
    "python",
    "pi_agent/rpc_server.py",
    "--config",
    str(trial / "pi_agent.llama-server.trial.json"),
]
orch["model"]["provider"] = "llama_server"
orch["model"]["model"] = "qwen35-9b"
orch.pop("max_steps", None)
orch["agent_context"] = {
    **orch.get("agent_context", {}),
    "enabled": True,
    "prompt_cache": True,
}
orch["decision_log"] = str(trial / "decisions.jsonl")
(trial / "orchestrator.llama-server.trial.json").write_text(
    json.dumps(orch, indent=2) + "\n",
    encoding="utf-8",
)
PY

echo "Trial directory: $trial_root"
echo "Memory worktree: $memory_worktree"
echo "Starting llama-server qwen35-9b sub-agent trial run..."

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

python -m pi_orchestrator.orchestrator \
  --config "$trial_root/orchestrator.llama-server.trial.json"
