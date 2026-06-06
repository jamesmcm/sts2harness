# sts2harness

State-scoped CLI wrapper for the STS2MCP mod API.

The raw STS2MCP MCP server exposes every tool all the time. This harness reads
the current `get_game_state(format=json)` payload and narrows it to the actions
that are legal on the current screen, following the UI flow documented in the
Slay the Spire 2 harness notes:

- combat: playable cards, legal targets, usable potions, end turn
- hand selection: selectable hand cards and confirm
- map: travelable map nodes
- event: visible unlocked options, or dialogue advance
- shop/fake merchant: stocked affordable purchases, with potion purchases hidden
  when potion slots are full, plus leave/proceed even while the inventory is open
- rewards: claimable rewards, with potion rewards hidden when potion slots are
  full, card reward pick/skip, proceed
- rest site: enabled rest options, proceed
- card selection: select, confirm, cancel for upgrade/transform/remove flows
- treasure, relic choice, bundle choice, Crystal Sphere, menu/game-over flows

## Usage

The STS2MCP mod must be loaded in the game and serving HTTP on port `15526`.

```bash
uv run python main.py snapshot
uv run python main.py actions
uv run python main.py act 'end_turn'
uv run python main.py submit 0
```

Use a different mod URL if needed:

```bash
uv run python main.py --base-url http://localhost:15526 actions
```

## Harness-Managed Run Setup

Create `sts2harness.json` in this directory to make seed, ascension, and
optional character selection harness policy rather than agent input:

```json
{
  "run_setup": {
    "seed_policy": "fixed_until_win",
    "seed": "ABC123",
    "ascension": 0,
    "max_ascension": 10,
    "stop_after_consecutive_a10_wins": 3,
    "character": "IRONCLAD",
    "progress_file": ".sts2harness-progress.json",
    "save_root": "~/.local/share/SlayTheSpire2/steam",
    "increment_ascension_on_win": true
  }
}
```

On the custom-run setup screen, `confirm` / `embark` actions automatically add
the configured `seed` and current persisted `ascension` to the STS2MCP request.
After that run-start action, `act` reads the newest `current_run.save` under
`save_root` and emits `run_setup_verification` showing expected vs actual
`seed`, `ascension`, `game_mode`, and save path.
After a `game_over` state, the harness reads the newest completed `.run` file
under `save_root` history saves and updates `progress_file` once for that run.
Wins increment ascension up to A10. A10 wins advance the seed when using a seed
set and stop the experiment after three consecutive A10 wins by default.

For official experiment setup, protected config, seed policies, SQLite logging,
and auto-resolve behavior, see [USER_GUIDE.md](USER_GUIDE.md).

Use another config path with:

```bash
uv run python main.py --config path/to/sts2harness.json act 0
```

The default HTTP timeout is 30 seconds. Override it for diagnostics:

```bash
uv run python main.py --timeout 60 act 0
```

The harness waits at least 1 second between STS2MCP HTTP calls by default,
including calls made by separate shell-chained harness processes. This reduces
main-thread queue pressure in the mod and gives game state snapshots time to
settle. Override it for diagnostics only:

```bash
uv run python main.py --mcp-delay 0 snapshot
```

`act` / `submit` waits 2 seconds before reading the post-action state. This is
intentional: combat turn transitions, map travel, strength/debuff updates, and
room-entry animations can lag behind the HTTP action response. Use `--wait 0`
only when the caller explicitly wants the immediate, possibly transitional
state.

## Output Model

`actions` returns:

```json
{
  "state_type": "monster",
  "actions": [
    {
      "index": 0,
      "id": "play_card:3:jaw_worm_0",
      "label": "Play hand[3] Strike on Jaw Worm",
      "category": "combat",
      "request": {
        "action": "play_card",
        "card_index": 3,
        "target": "JAW_WORM_0"
      }
    }
  ]
}
```

`act ACTION` / `submit ACTION` re-reads state, rebuilds the legal action set, and
only dispatches the action if the ID or numeric action index is still legal. It
then returns the post-action state and next legal actions by default. This
prevents stale card/reward indices from being reused after a previous action
shifted the UI.

If STS2MCP times out while executing an action, `act` returns structured JSON
instead of a traceback:

```json
{
  "result": {
    "status": "timeout_uncertain",
    "error": "Timed out waiting for STS2MCP to respond..."
  },
  "state": {},
  "actions": []
}
```

Treat this as an uncertain action result: inspect the returned state/actions
before retrying, because the game may still have applied the command.
