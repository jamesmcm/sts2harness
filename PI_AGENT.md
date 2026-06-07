# Pi Agent RPC Mode

`pi_agent` is a Python-only JSON-RPC stdio server for agents that should use
the harness library directly instead of shelling out to `main.py`. TypeScript is
not needed for this path unless the external Pi orchestrator is itself a Node
program.

## Files

- `pi_agent/rpc_server.py`: JSON-RPC server exposing harness actions and memory
  file tools.
- `pi_orchestrator/orchestrator.py`: external model loop that talks to the RPC
  server, asks a CLI-backed LLM for one legal action, applies it, and updates
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

Example memory write:

```json
{"jsonrpc":"2.0","id":3,"method":"append_memory","params":{"path":"BATTLE_LOG.md","content":"\nFloor 3: took 2 damage, added Strike+.\n"}}
```

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
- `append_memory`: appends to one memory file under `memory_root`.

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
8. Append a compact decision record to `decision_log`.

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

### OpenCode Go / Other OpenAI-Compatible APIs

Use `provider: "openai_compatible_chat"` if OpenCode Go, or another provider,
gives you an OpenAI-compatible `/chat/completions` endpoint and API key. This
also does not use the OpenCode CLI.

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

The model string and base URL depend on the provider. If OpenCode Go does not
offer an OpenAI-compatible API endpoint, we need its actual API documentation
before wiring it in.

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
call an LLM itself, collect token usage, or decide strategy. The external Pi
orchestrator is still responsible for model calls, prompt construction, tool
policy, run scheduling, and writing token/model-call counts into the database in
a later integration.
