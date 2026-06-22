#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

timestamp="$(date +%Y%m%d-%H%M%S)"
trial_root="${TRIAL_ROOT:-$repo_root/.trial-opencode-go-$timestamp}"
memory_repo="$trial_root/agent-memory-source"
memory_worktree="$trial_root/agent-memory-worktree"
memory_branch="${MEMORY_BRANCH:-memory/opencode-go-deepseek-v4-flash-trial}"

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

harness = json.loads(pathlib.Path("sts2harness.opencode-go.json").read_text())
seed_file = pathlib.Path(harness["run_setup"]["seed_file"]).expanduser()
if not seed_file.is_absolute():
    seed_file = pathlib.Path.cwd() / seed_file
harness["run_setup"]["seed_file"] = str(seed_file)
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
(trial / "sts2harness.opencode-go.trial.json").write_text(
    json.dumps(harness, indent=2) + "\n",
    encoding="utf-8",
)

pi = json.loads(pathlib.Path("pi_agent/config/pi_agent.opencode-go.local.json").read_text())
pi["harness_config"] = str(trial / "sts2harness.opencode-go.trial.json")
pi["memory_root"] = str(worktree)
(trial / "pi_agent.opencode-go.trial.json").write_text(
    json.dumps(pi, indent=2) + "\n",
    encoding="utf-8",
)

orch = json.loads(
    pathlib.Path("pi_orchestrator/config/orchestrator.opencode-go.local.json").read_text()
)
orch["rpc_server_command"] = [
    "python",
    "pi_agent/rpc_server.py",
    "--config",
    str(trial / "pi_agent.opencode-go.trial.json"),
]
orch.pop("max_steps", None)
orch["decision_log"] = str(trial / "decisions.jsonl")
(trial / "orchestrator.opencode-go.trial.json").write_text(
    json.dumps(orch, indent=2) + "\n",
    encoding="utf-8",
)
PY

echo "Trial directory: $trial_root"
echo "Memory worktree: $memory_worktree"
echo "Starting OpenCode Go DeepSeek V4 Flash trial run..."

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
  --config "$trial_root/orchestrator.opencode-go.trial.json"
