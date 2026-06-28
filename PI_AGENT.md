# Pi Agent RPC Mode

`pi_agent` is a Python-only JSON-RPC stdio server for agents that should use
the harness library directly instead of shelling out to `main.py`. TypeScript is
not needed for this path unless the external Pi orchestrator is itself a Node
program.

## Files

- `pi_agent/rpc_server.py`: JSON-RPC server exposing harness actions and memory
  file tools.
- `pi_orchestrator/orchestrator.py`: external model loop that talks to the RPC
  server, asks a hosted LLM for one legal action, applies it, and updates
  memory.
- `pi_agent/config/pi_agent.example.json`: runtime config for the RPC server.
- `pi_agent/config/official_harness.example.json`: trusted harness config for
  official Pi-agent experiments.
- `pi_agent/config/experiment_schedule.example.json`: condition/LLM schedule
  template for balanced interleaving.
- `pi_agent/memory/`: example agent-writable memory files.

## Security Model

The agent may write memory files. It must not be able to modify:

- `main.py`
- `pi_agent/rpc_server.py`
- the official harness config
- the progress file
- the SQLite run database
- the wrapper or daemon that launches the RPC server

The clean setup is two OS users:

- `sts2exp`: owns harness code, trusted config, progress, logs, and database.
- `sts2agent`: owns only the agent workspace and memory files.

Example directory layout:

```text
/opt/sts2harness/                         owned by sts2exp, read-only to agents
/var/lib/sts2harness/official/            owned by sts2exp, mode 750
/var/lib/sts2harness/agents/pi-agent/     memory writable by RPC server and agent
```

If the agent runs as the same OS user that owns the harness repo, it can tamper
with config and code. A separate directory alone is not a security boundary.

## Installing Trusted Files

Example:

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin sts2exp
sudo mkdir -p /opt/sts2harness /var/lib/sts2harness/official
sudo rsync -a --delete /home/archie/repos/personal/github/sts2harness/ /opt/sts2harness/
sudo cp /opt/sts2harness/pi_agent/config/official_harness.example.json \
  /var/lib/sts2harness/official/sts2harness.json
sudo chown -R sts2exp:sts2exp /opt/sts2harness /var/lib/sts2harness/official
sudo chmod -R go-w /opt/sts2harness
sudo chmod 750 /var/lib/sts2harness/official
```

Create memory writable by the RPC server and, if desired, by the agent user via
a shared group:

```bash
sudo groupadd --system sts2mem
sudo usermod -aG sts2mem sts2exp
sudo usermod -aG sts2mem sts2agent
sudo mkdir -p /var/lib/sts2harness/agents/pi-agent/memory
sudo chown -R sts2exp:sts2mem /var/lib/sts2harness/agents/pi-agent
sudo chmod -R 770 /var/lib/sts2harness/agents/pi-agent
```

Then create `/var/lib/sts2harness/official/pi_agent.json`:

```json
{
  "harness_config": "/var/lib/sts2harness/official/sts2harness.json",
  "memory_root": "/var/lib/sts2harness/agents/pi-agent/memory",
  "base_url": "http://localhost:15526",
  "timeout": 30,
  "mcp_delay": 1.0,
  "wait_after_action": 2.0
}
```

Owned by `sts2exp`, mode `640` or stricter.

## Running RPC Mode

Start the server:

```bash
cd /opt/sts2harness
sudo -u sts2exp uv run python pi_agent/rpc_server.py \
  --config /var/lib/sts2harness/official/pi_agent.json
```

The protocol is newline-delimited JSON-RPC 2.0 over stdin/stdout.

Example request:

```json
{"jsonrpc":"2.0","id":1,"method":"snapshot","params":{}}
```

Example action:

```json
{"jsonrpc":"2.0","id":2,"method":"act","params":{"action":0}}
```

Example memory rewrite:

```json
{"jsonrpc":"2.0","id":3,"method":"write_memory","params":{"path":"CURRENT_RUN.md","content":"# Current Run\n\nSeed: R58NTKJSSE, A0 Ironclad.\n\n## Plan\nMaintain the full active-run tactical sheet here.\n"}}
```

## Memory Git Worktrees

For official runs, put the memory files in a git worktree owned by the trusted
harness/RPC user. Configure:

- `logging.memory_git_source`: the source memory repository.
- `logging.memory_git_dir`: the per-condition worktree containing
  `STRATEGY.md`, `CURRENT_RUN.md`, `BATTLE_LOG.md`, and
  `HARNESS_BUGS.md`.
- `logging.memory_git_branch`: a stable branch name for this condition.
- `logging.memory_git_init`: whether the harness may initialize the source repo
  if it does not exist.
- `logging.memory_commit_on_run_start`: commit a baseline snapshot when the run
  is logged.
- `logging.memory_commit_on_room_change`: commit once per floor/room checkpoint.
- `logging.memory_commit_on_run_end`: commit the final run-end memory snapshot.

The model does not run git. It only edits memory files through RPC. The harness
commits the configured paths from the trusted side.

## RPC Methods

- `ping`: health check.
- `raw_state`: calls STS2MCP state directly; params may include
  `{"format": "json"}` or `{"format": "markdown"}`.
- `actions`: returns legal actions after auto-resolving trivial transitions.
- `snapshot`: returns state plus legal actions after auto-resolve.
- `act`: submits a legal action by index or ID and returns the next state.
- `list_memory`: lists files under `memory_root`.
- `read_memory`: reads one memory file under `memory_root`.
- `write_memory`: replaces one memory file under `memory_root`.
- `append_memory`: disabled; maintain memory through full-file `write_memory`
  rewrites.
- `record_model_telemetry`: records Pi orchestrator prompt/response hashes,
  full prompt/response payloads, token usage, request/response IDs, pricing
  metadata, and RPC tool-call counts against the latest logged agent step.

Memory paths are resolved under `memory_root`; `../` escapes are rejected.

## Pi Orchestrator

The RPC server does not call a model by itself. The orchestrator does that:

```bash
cd /opt/sts2harness
uv run python pi_orchestrator/orchestrator.py \
  --config pi_orchestrator/config/orchestrator.openai.example.json
```

For local debugging from this checkout:

```bash
uv run python pi_orchestrator/orchestrator.py \
  --config pi_orchestrator/config/orchestrator.local.json
```

The orchestrator loop is:

1. Start `pi_agent/rpc_server.py`.
2. Call `snapshot`.
3. Read `STRATEGY.md`, `CURRENT_RUN.md`, and `BATTLE_LOG.md`.
4. Ask the configured CLI LLM to return JSON with one legal `action_ref`.
5. Validate that the action is still legal.
6. Apply memory updates.
7. Call `act`.
8. Record model telemetry in the harness SQLite database when official logging
   is active.
9. Commit memory checkpoints from the trusted harness side when configured.
10. Append a compact decision record to `decision_log`.

`act` always returns a fresh post-action state/actions payload because allowing
blind follow-up commands makes stale action queues too easy.

The current Pi loop does not summarize context or spawn subagents. It passes
the full snapshot plus the full contents of `STRATEGY.md`, `CURRENT_RUN.md`,
and `BATTLE_LOG.md` into each decision prompt, then applies any memory updates
the model returns. A summarizing/subagent Pi condition should be implemented as
a separate orchestrator variant so it can be measured as an ablation.

For OpenRouter providers, the orchestrator records the returned completion ID
as the generation ID and queries `/api/v1/generation` plus
`/api/v1/generation/content` for cost, request ID, upstream ID, native token
counts, provider metadata, and stored prompt/completion content when OpenRouter
has those records available.

### OpenAI / ChatGPT

Use `provider: "openai_responses"` to call the OpenAI API directly. This does
not use the Codex CLI or your local Codex session. It needs a Platform API key:

Example:

```json
{
  "model": {
    "provider": "openai_responses",
    "model": "gpt-5",
    "api_key_env": "OPENAI_API_KEY",
    "base_url": "https://api.openai.com/v1",
    "timeout": 300
  }
}
```

Set the model by changing `model.model`. Set the key in the environment before
starting the orchestrator:

```bash
export OPENAI_API_KEY='...'
```

ChatGPT paid subscriptions and OpenAI API usage are separate products. A
ChatGPT account can be used to create/manage Platform API keys if the account
has API access and billing configured, but the orchestrator needs the API key,
not the ChatGPT web session or Codex CLI login.

### OpenCode Go

Use `provider: "opencode_go"` to call OpenCode Go directly from the harness
with an OpenCode Go API key. This does not use the OpenCode CLI or Pi extension.
The provider defaults to `model: "deepseek-v4-flash"`,
`api_key_env: "OPENCODE_API_KEY"`, and
`base_url: "https://opencode.ai/zen/go/v1"`. When agent context is enabled,
the default compaction threshold is derived from the 1M-token model window and
is set around 800k tokens unless overridden.

Example:

```json
{
  "model": {
    "provider": "opencode_go",
    "model": "deepseek-v4-flash",
    "api_key_env": "OPENCODE_API_KEY",
    "timeout": 300
  }
}
```

Set:

```bash
export OPENCODE_API_KEY='...'
```

The model name is the raw OpenCode Go API model ID. For example, the
`pi-opencode-bridge` Pi package exposes this same model in Pi as
`oc-sdk-go/deepseek-v4-flash`, where `oc-sdk-go` is the Pi provider prefix and
`deepseek-v4-flash` is the API model ID used here.

### llama-server

Use `provider: "llama_server"` to call a local llama.cpp `llama-server`
OpenAI-compatible `/chat/completions` endpoint. The provider defaults to
`model: "qwen35-9b"`, `base_url: "http://127.0.0.1:8080/v1"`, no API key,
`cache_prompt: true`, `max_tokens: 20000`, and a 32k context window with
compaction at about 24k tokens.

Example config:

```text
pi_orchestrator/config/orchestrator.llama-server.local.json
```

To start an isolated local sub-agent trial run with the `qwen35-9b`
llama-server model:

```bash
scripts/run_llama_server_qwen35_trial.sh
```

The provider sends `cache_prompt: true` so llama-server can reuse its prompt
cache. The checked-in local config also sends `id_slot: 0`, which pins this
single local harness to one llama-server slot. Remove `id_slot` when sharing the
same server with other clients. If llama-server logs `prompt_save` and `looking
for better prompt`, automatic prompt caching is already active; a later `forcing
full prompt re-processing` usually means the common prefix was shorter than the
available checkpoint, or the model/server cannot restore that checkpoint. In
that case, prefer improving stable prompt prefix reuse or server checkpoint/SWA
settings before adding explicit slot save/restore calls.

The orchestrator treats empty chat content as a retryable model response
failure. It also retries rejected decisions with a correction prompt that
includes the exact validation error, the previous decision JSON, and the legal
actions list. The local llama-server config enables `agent_context` by default,
so map/pathing and battle/tactical context are split across sub-agent roles. The
provider supports `thinking_mode`:

```json
{
  "model": {
    "provider": "llama_server",
    "model": "qwen35-9b",
    "thinking_mode": "auto",
    "no_thinking_action_threshold": 2
  }
}
```

`thinking_mode: "auto"` disables thinking when there are one or two legal
actions and enables it when there are more. Use `"enabled"` or `"disabled"` to
force one behavior for every call.

### Other OpenAI-Compatible APIs

Use `provider: "openai_compatible_chat"` for other providers that expose an
OpenAI-compatible `/chat/completions` endpoint and API key.

Example:

```json
{
  "model": {
    "provider": "openai_compatible_chat",
    "model": "MODEL_NAME_HERE",
    "api_key_env": "OPENCODE_API_KEY",
    "base_url": "https://OPENAI_COMPATIBLE_BASE_URL_HERE/v1",
    "timeout": 300
  }
}
```

Set:

```bash
export OPENCODE_API_KEY='...'
```

The model string, key environment variable, and base URL depend on the provider.

### OpenRouter / Kimi K2.6

Use `provider: "openrouter"` for OpenRouter. It defaults to
`https://openrouter.ai/api/v1` and `OPENROUTER_API_KEY`.

Example config:

```text
pi_orchestrator/config/orchestrator.openrouter-kimi.example.json
```

As of June 14, 2026, OpenRouter lists Kimi K2.6 as:

```text
moonshotai/kimi-k2.6
```

## Running the Experiments

1. Start STS2 with STS2MCP loaded.
2. Install trusted harness config under `/var/lib/sts2harness/official`.
3. Fill `seed_set` with the fixed seed list shared by every LLM/condition.
4. For each LLM and condition, create a separate official harness config with:
   - distinct `agent.agent_name`
   - distinct `agent.model_name`
   - distinct `agent.condition_name`
   - distinct `run_setup.progress_file`
   - shared `logging.sqlite_path`
   - shared seed list/order
5. Interleave conditions using `experiment_schedule.example.json`; do not run
   all baseline runs before memory/tool runs.
6. Launch the Pi RPC server for the current condition.
7. Launch the Pi orchestrator with the matching OpenAI or OpenAI-compatible
   provider config.
8. Continue until the progress file marks `stopped: true`.

The harness handles:

- custom-run seed and ascension injection
- new-run verification from `current_run.save`
- progress updates from completed `.run` save history
- ascension increments on wins
- seed advancement after A10 wins when using a seed set
- stop after three consecutive A10 wins
- SQLite run/step logging for verified new official runs
- trivial auto-resolve before observations reach the agent
- git memory checkpoints at run start, room changes, and run end when enabled

## Giving Other Agents Harness Access

For CLI-based agents, provide a wrapper command owned by the trusted user:

```bash
#!/usr/bin/env bash
cd /opt/sts2harness || exit 1
exec sudo -u sts2exp uv run python main.py \
  --config /var/lib/sts2harness/official/sts2harness.json \
  "$@"
```

Then give the agent permission to execute only that wrapper, not to edit the
repo or trusted config. In a stricter deployment, replace `sudo` with a small
daemon that owns the config and exposes only `snapshot/actions/act` over a Unix
socket. Firejail/LXC can work, but test carefully because broad tool sandboxes
often interfere with the agent's normal file tools. The important boundary is
that the process performing harness actions runs with read access to trusted
config and write access to progress/logs, while the agent process does not.

## Current Limitations

The RPC server exposes harness/game actions and memory file tools. It does not
call an LLM itself or decide strategy. The external Pi orchestrator is still
responsible for model calls, prompt construction, tool policy, run scheduling,
and reporting telemetry back to the harness.
