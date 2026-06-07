# CLI Agent Experiment Guide

This directory is for agents that use the `sts2harness` CLI directly. This is
separate from `pi_agent`, which exposes JSON-RPC, and separate from
`pi_orchestrator`, which owns its own model loop.

CLI agents include Codex, OpenCode, Hermes, oh-my-pi, and any other tool that
can run shell commands. The agent should call only the wrapper command you give
it, for example:

```bash
sts2harness snapshot
sts2harness actions
sts2harness act 0
sts2harness act end_turn
```

The agent must not receive direct write access to the trusted harness config,
progress file, SQLite database, or harness source code.

## Trust Boundary

Use two roles:

- Trusted harness role: owns `main.py`, trusted config, progress file, and
  SQLite database.
- Agent role: can run the wrapper and write only its own memory/strategy files.

Recommended filesystem layout:

```text
/opt/sts2harness/                         trusted code, read-only to agents
/var/lib/sts2harness/official/            trusted configs/progress/db
/var/lib/sts2harness/agents/codex/        Codex memory/workspace
/var/lib/sts2harness/agents/opencode/     OpenCode memory/workspace
```

If the agent runs as the same OS user that owns the harness checkout, it can
modify code/config. That is acceptable for debugging but not for official runs.
For official runs, use a separate OS user, a daemon, or a locked-down wrapper.

## Protected Harness Config

Start from:

```text
cli_agents/config/official_cli_harness.example.json
```

Copy it to a protected path:

```bash
sudo mkdir -p /var/lib/sts2harness/official
sudo cp cli_agents/config/official_cli_harness.example.json \
  /var/lib/sts2harness/official/sts2harness-codex.json
sudo chown sts2exp:sts2exp /var/lib/sts2harness/official/sts2harness-codex.json
sudo chmod 640 /var/lib/sts2harness/official/sts2harness-codex.json
```

Create one config per agent/condition. Keep the seed list order identical across
conditions, but use distinct `progress_file` values so conditions do not share
ascension/seed state.

## Wrapper

Install a wrapper that always uses the protected config:

```bash
sudo install -m 755 cli_agents/wrappers/sts2harness-wrapper.example.sh \
  /usr/local/bin/sts2harness-codex
```

Edit the installed wrapper paths for the specific condition:

```bash
HARNESS_ROOT=/opt/sts2harness
HARNESS_CONFIG=/var/lib/sts2harness/official/sts2harness-codex.json
```

Agents should call `sts2harness-codex`, not `python main.py` directly.

For a stronger boundary, make the wrapper talk to a small daemon owned by
`sts2exp`. A shell wrapper with `sudo -u sts2exp` is workable if sudoers allows
only the exact harness command and config path.

## Codex Direct Run

Use Codex as the game-playing agent, not the Pi orchestrator:

```bash
codex exec \
  --cd /var/lib/sts2harness/agents/codex \
  --sandbox workspace-write \
  --ask-for-approval never \
  --model gpt-5 \
  < cli_agents/prompts/codex_cli_agent.md
```

The Codex workspace should contain only agent-writable files such as:

```text
STRATEGY.md
CURRENT_RUN.md
BATTLE_LOG.md
```

The prompt tells Codex to use `sts2harness-codex` for game actions and to keep
its memory files in its workspace.

## OpenCode Direct Run

Use OpenCode as the game-playing agent:

```bash
opencode run \
  --dir /var/lib/sts2harness/agents/opencode \
  --model PROVIDER/MODEL \
  --agent build \
  "$(cat cli_agents/prompts/opencode_cli_agent.md)"
```

If the OpenCode agent profile should not have broad tools, configure that in
OpenCode itself. The important harness-side rule is the same: the only game
control entrypoint it receives is the wrapper command.

## Agent Instructions

Give every CLI agent these rules:

- Use only `sts2harness-*` wrapper commands for game state/actions.
- Never call STS2MCP endpoints directly.
- Never edit harness config, harness code, progress files, or SQLite DBs.
- You may edit only your memory/strategy files in your own workspace.
- Use `snapshot` before choosing actions unless you just received a fresh
  post-action state.
- Use `act ACTION` with the current legal action index or ID.
- Treat `auto_actions` as already handled by the harness.
- Stop if no legal actions are available or if the run is marked stopped.

## Experiment Flow

1. Start STS2 with STS2MCP loaded.
2. Install trusted harness code/config under protected paths.
3. Create one wrapper per agent/condition.
4. Create one agent workspace per agent/condition.
5. Launch conditions in an interleaved schedule, not all one condition first.
6. Let each agent start new custom runs through the harness.
7. The harness injects seed/ascension, verifies `current_run.save`, logs official
   new runs, auto-resolves trivial transitions, and updates progress from save
   history after game over.

## Local Debugging

For local debugging without a separate OS user, from this checkout:

```bash
uv run python main.py --config sts2harness.json snapshot
uv run python main.py --config sts2harness.json actions
uv run python main.py --config sts2harness.json act 0
```

This is not a secure official setup because the agent could modify local config
or code if it has workspace write access.

