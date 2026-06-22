#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
trial_root="$repo_root/.trial-opencode-go-20260619-003920"

exec "$repo_root/scripts/resume_opencode_go_trial.sh" "$trial_root"
