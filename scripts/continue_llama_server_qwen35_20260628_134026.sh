#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
trial_root="$repo_root/.trial-llama-server-qwen35-9b-subagent-20260628-134026"

exec "$repo_root/scripts/resume_llama_server_qwen35_trial.sh" "$trial_root"
