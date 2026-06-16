# STS2 Harness Experiment Guide

This guide describes the experiment setup where agents use `sts2harness` to play
STS2 through STS2MCP, while the harness owns seed, ascension, run logging, and
trivial UI transitions.

## Trust Boundary

The experiment config and progress file must live outside the agent-writable
workspace. If an agent can edit `sts2harness.json` or the progress file, it can
change seeds, ascension, or stop conditions.

Recommended setup:

1. Create a separate OS user, for example `sts2exp`, that owns the trusted
   config, progress file, and SQLite database.
2. Put the config under a directory agents cannot write, for example
   `/var/lib/sts2harness/official/sts2harness.json`.
3. Put agent memory/strategy files somewhere the agent can write, separate from
   the trusted harness config.
4. Expose a wrapper command to the agent that always passes the protected config
   path.

Example wrapper:

```bash
#!/usr/bin/env bash
exec /home/archie/.local/bin/uv \
  --directory /home/archie/repos/personal/github/sts2harness \
  run python main.py \
  --config /var/lib/sts2harness/official/sts2harness.json \
  "$@"
```

For a hard boundary, run the wrapper through a daemon or `sudo -u sts2exp`
instead of letting the same OS user own both the agent files and the config. A
plain separate directory is useful for organization, but Unix permissions only
protect it if the agent process is a different user or otherwise sandboxed.

## Trusted Config

Example official-run config:

```json
{
  "agent": {
    "agent_name": "codex",
    "model_name": "gpt-5-medium",
    "condition_name": "strong_prompt_memory_files",
    "prompt_version": "2026-06-06-a",
    "memory_version": "strategy-v1"
  },
  "run_setup": {
    "seed_policy": "fixed_list_until_win",
    "seed_set": ["R58NTKJSSE", "ABCD1234", "KLMN5678"],
    "ascension": 0,
    "max_ascension": 10,
    "stop_after_consecutive_a10_wins": 3,
    "stop_after_current_run": false,
    "character": "IRONCLAD",
    "progress_file": "/var/lib/sts2harness/official/progress.json",
    "save_root": "~/.local/share/SlayTheSpire2/steam",
    "increment_ascension_on_win": true
  },
  "logging": {
    "official_run_logging": true,
    "sqlite_path": "/var/lib/sts2harness/official/runs.sqlite",
    "harness_version": "sts2harness-2026-06-06",
    "memory_git_dir": "/var/lib/sts2harness/official/agent-worktree",
    "memory_git_source": "/var/lib/sts2harness/official/agent-memory.git",
    "memory_git_branch": "memory/codex-strong-prompt-memory-files",
    "memory_git_init": true,
    "memory_commit_on_run_start": true,
    "memory_commit_on_room_change": true,
    "memory_commit_on_run_end": true,
    "memory_commit_paths": [
      "STRATEGY.md",
      "CURRENT_RUN.md",
      "BATTLE_LOG.md",
      "HARNESS_BUGS.md"
    ]
  },
  "auto_resolve": {
    "enabled": true,
    "max_actions": 10
  }
}
```

The harness updates `progress_file` itself. Agents should never receive write
access to that file.

## Seed Policies

Supported values:

- `fixed`: use the same configured `seed` every run.
- `fixed_until_win`: use the same configured `seed`; losses repeat it, wins
  advance ascension.
- `fixed_list`: use `seed_set` in order and advance after every completed run.
- `fixed_list_until_win`: use `seed_set` in order; losses repeat the current
  seed, wins advance.

For all policies, a win below A10 increments ascension by one, up to
`max_ascension`. An A10 win increments `consecutive_a10_wins` and advances the
seed when a `seed_set` exists. After
`stop_after_consecutive_a10_wins` consecutive A10 wins, the progress file is
marked stopped and the custom-run start action is no longer offered.
Set `stop_after_current_run` to `true` when you want the current in-progress
run to be the final run in the experiment; the harness marks progress stopped
after the next `game_over` state.

When `logging.memory_git_source` is set, the harness treats `memory_git_dir` as
the condition worktree and creates it from that source repository if needed.
Use one branch/worktree per agent condition. When
`memory_commit_on_run_start`, `memory_commit_on_room_change`, or
`memory_commit_on_run_end` are enabled, the harness commits the configured
`memory_commit_paths` inside `memory_git_dir` at those checkpoints. Keep SQLite
databases outside that git worktree.

## Running Agents

STS2MCP and the game must already be running. The CLI does not launch an agent;
agents call the harness command to inspect and act.

Common commands:

```bash
sts2harness snapshot
sts2harness actions
sts2harness act 0
sts2harness act end_turn
```

When the agent selects `custom`, the harness exposes the configured character
and start button. The start action automatically includes the trusted seed and
ascension:

```json
{
  "action": "menu_select",
  "option": "confirm",
  "seed": "R58NTKJSSE",
  "ascension": 0
}
```

After a new run starts, the harness reads the newest `current_run.save` under
`save_root` and emits `run_setup_verification`. This confirms the seed,
ascension, game mode, start time, and save path. No Compendium API calls are
used because that endpoint can hang.

## Auto-Resolve

With `auto_resolve.enabled`, the harness consumes deterministic transitions
before returning observations to the agent. The agent sees the resulting state
and an `auto_actions` list recording what happened.

Currently auto-resolved:

- lone `proceed_to_map`
- event dialogue advance
- Crystal Sphere proceed
- a single available map node

The harness does not auto-pick cards, rewards, shop purchases, event choices,
rest options, relic choices, or combat actions.

## Progress Updates

Progress is updated only after a `game_over` state. The harness reads the newest
completed `.run` file from `saves/history` under `save_root`, processes it once,
and writes:

- current ascension
- current seed index
- last completed run identity/path
- last completed seed and ascension
- victory/loss
- consecutive A10 wins
- stopped/stop reason

`current_run.save` is used only to verify new run setup. It may lag during a
live run, especially until entering a new floor.

## SQLite Logging

Logging starts only for verified new official runs. Attached/debug sessions are
not logged unless the config has the full official setup:

- `logging.official_run_logging: true`
- `logging.sqlite_path`
- `agent.agent_name`
- `agent.model_name`
- `agent.condition_name`
- configured seed and ascension

The run ID includes agent, condition, seed, ascension, and run start time. The
database contains:

- `runs`: one row per official run, including seed, ascension, model, condition,
  character, final floor, victory, aggregate action counts, and Pi orchestrator
  model/token/tool-call/cost totals when available.
- `steps`: one row per logged agent or auto action, including state stats,
  legal actions, chosen action, action source, observation hash, and Pi
  orchestrator prompt/response hashes plus full prompt/response payloads when
  available.
- `run_summaries`: reserved for later agent/harness summaries and memory diffs.

For Pi orchestrator runs, model calls, input/output tokens, full prompt text,
full response text, raw request/response JSON, prompt/response hashes, and RPC
tool-call counts are recorded after each agent action.
For OpenRouter runs, the orchestrator also records the response/generation ID
and queries OpenRouter's generation endpoints for request ID, upstream ID,
provider, native prompt/completion tokens, total cost, and stored
prompt/completion content when available. `prompt_cost` and `completion_cost`
are a proportional split from `total_cost` and native token counts when
OpenRouter does not provide separate billed prompt/completion costs.
For CLI-agent runs, those fields still require the CLI wrapper or external
agent to report telemetry.

## Pi Agent Library Use

The Pi agent can import the harness code directly instead of shelling out:

```python
import main as sts2harness

config = sts2harness.load_harness_config("/var/lib/sts2harness/official/sts2harness.json")
client = sts2harness.Sts2Client()
state = sts2harness._wait_for_play_phase(client)
state, auto_actions = sts2harness.resolve_auto_actions(client, state, config)
actions = sts2harness.build_actions(state, config.run_setup)
```

The direct library path should use the same protected config/progress files as
the CLI path. Strategy, tactics, and memory documents can remain agent-writable
because they are not trusted experiment controls.

## Restarting

Harness changes do not require restarting the game or STS2MCP. Restart only if
the mod itself was rebuilt or reinstalled. For this harness-only setup, start a
fresh run through the harness after updating the config.
