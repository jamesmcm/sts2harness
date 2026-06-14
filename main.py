from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_BASE_URL = "http://localhost:15526"
DEFAULT_MCP_DELAY = 1.0
DEFAULT_THROTTLE_FILE = "/tmp/sts2harness-mcp-throttle"
DEFAULT_CONFIG_FILE = "sts2harness.json"
DEFAULT_PROGRESS_FILE = ".sts2harness-progress.json"
DEFAULT_SAVE_ROOT = "~/.local/share/SlayTheSpire2/steam"
DEFAULT_SEED_POLICY = "fixed_until_win"
SEED_POLICIES = {"fixed", "fixed_until_win", "fixed_list", "fixed_list_until_win"}


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class RunSetup:
    seed: str | None = None
    seed_set: tuple[str, ...] = ()
    seed_policy: str = DEFAULT_SEED_POLICY
    ascension: int | None = None
    max_ascension: int = 10
    character: str | None = None
    progress_file: str = DEFAULT_PROGRESS_FILE
    save_root: str = DEFAULT_SAVE_ROOT
    increment_ascension_on_win: bool = True
    stop_after_consecutive_a10_wins: int = 3
    stop_after_current_run: bool = False


@dataclass(frozen=True)
class AgentConfig:
    agent_name: str | None = None
    model_name: str | None = None
    condition_name: str | None = None
    prompt_version: str | None = None
    memory_version: str | None = None
    memory_checksum: str | None = None


@dataclass(frozen=True)
class LoggingConfig:
    sqlite_path: str | None = None
    official_run_logging: bool = False
    harness_version: str | None = None
    git_commit: str | None = None
    memory_git_dir: str | None = None
    memory_commit_on_run_start: bool = False
    memory_commit_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessConfig:
    run_setup: RunSetup
    agent: AgentConfig = AgentConfig()
    logging: LoggingConfig = LoggingConfig()
    auto_resolve: bool = True
    max_auto_actions: int = 10


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    category: str
    request: JsonDict
    notes: tuple[str, ...] = ()
    enabled: bool = True

    def as_dict(self, index: int | None = None) -> JsonDict:
        result: JsonDict = {
            "id": self.id,
            "label": self.label,
            "category": self.category,
            "enabled": self.enabled,
            "request": self.request,
        }
        if index is not None:
            result = {"index": index, **result}
        if self.notes:
            result["notes"] = list(self.notes)
        return result


class Sts2Client:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 30.0,
        mcp_delay: float = DEFAULT_MCP_DELAY,
        throttle_file: str = DEFAULT_THROTTLE_FILE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.mcp_delay = max(0.0, mcp_delay)
        self.throttle_file = throttle_file

    @property
    def singleplayer_url(self) -> str:
        return f"{self.base_url}/api/v1/singleplayer"

    def _wait_for_request_slot(self) -> None:
        if self.mcp_delay <= 0:
            return

        throttle_dir = os.path.dirname(self.throttle_file)
        if throttle_dir:
            os.makedirs(throttle_dir, exist_ok=True)
        with open(self.throttle_file, "a+", encoding="utf-8") as throttle:
            fcntl.flock(throttle.fileno(), fcntl.LOCK_EX)
            throttle.seek(0)
            raw_last_request = throttle.read().strip()
            try:
                last_request_at = float(raw_last_request)
            except ValueError:
                last_request_at = 0.0

            elapsed = time.monotonic() - last_request_at
            if elapsed < self.mcp_delay:
                time.sleep(self.mcp_delay - elapsed)

            throttle.seek(0)
            throttle.truncate()
            throttle.write(str(time.monotonic()))
            throttle.flush()
            os.fsync(throttle.fileno())

    def get_state(self, *, response_format: str = "json") -> Any:
        self._wait_for_request_slot()
        query = urllib.parse.urlencode({"format": response_format})
        url = f"{self.singleplayer_url}?{query}"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        if response_format == "json":
            return json.loads(body)
        return body

    def post_action(self, request: JsonDict) -> JsonDict:
        self._wait_for_request_slot()
        data = json.dumps(request).encode("utf-8")
        http_request = urllib.request.Request(
            self.singleplayer_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)

    def get_compendium(self) -> JsonDict:
        self._wait_for_request_slot()
        url = f"{self.base_url}/api/v1/compendium"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:
            body = response.read().decode("utf-8")
        return json.loads(body)


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (TimeoutError, socket.timeout))
    return False


def _name(value: JsonDict, *keys: str, fallback: str = "unknown") -> str:
    for key in keys:
        item = value.get(key)
        if item is not None and str(item):
            return str(item)
    return fallback


def _load_json_file(path: str) -> JsonDict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _write_json_file(path: str, value: JsonDict) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid integer setting")
    return int(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if not isinstance(value, list):
        raise ValueError("seed_set must be a list of seed strings")
    seeds = tuple(str(item).strip() for item in value if str(item).strip())
    return seeds


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_harness_config(config_path: str) -> HarnessConfig:
    config = _load_json_file(config_path)
    run_config = config.get("run_setup")
    if not isinstance(run_config, dict):
        run_config = config

    seed = run_config.get("seed")
    seed_set = _string_tuple(run_config.get("seed_set") or run_config.get("seeds"))
    seed_policy = str(run_config.get("seed_policy") or DEFAULT_SEED_POLICY).strip()
    if seed_policy not in SEED_POLICIES:
        raise ValueError(
            f"seed_policy must be one of {', '.join(sorted(SEED_POLICIES))}"
        )
    character = run_config.get("character")
    progress_file = str(run_config.get("progress_file") or DEFAULT_PROGRESS_FILE)
    save_root = str(run_config.get("save_root") or DEFAULT_SAVE_ROOT)
    max_ascension = _optional_int(run_config.get("max_ascension"))
    if max_ascension is None:
        max_ascension = 10
    stop_after_a10 = _optional_int(run_config.get("stop_after_consecutive_a10_wins"))
    if stop_after_a10 is None:
        stop_after_a10 = 3
    stop_after_current = bool(run_config.get("stop_after_current_run"))
    increment_on_win = run_config.get("increment_ascension_on_win")
    if increment_on_win is None:
        increment_on_win = True

    setup = RunSetup(
        seed=str(seed).strip() if seed is not None and str(seed).strip() else None,
        seed_set=seed_set,
        seed_policy=seed_policy,
        ascension=_optional_int(run_config.get("ascension")),
        max_ascension=max_ascension,
        character=str(character).strip().upper()
        if character is not None and str(character).strip()
        else None,
        progress_file=progress_file,
        save_root=save_root,
        increment_ascension_on_win=bool(increment_on_win),
        stop_after_consecutive_a10_wins=stop_after_a10,
        stop_after_current_run=stop_after_current,
    )

    env_seed = os.environ.get("STS2HARNESS_SEED")
    env_ascension = os.environ.get("STS2HARNESS_ASCENSION")
    env_character = os.environ.get("STS2HARNESS_CHARACTER")
    env_save_root = os.environ.get("STS2HARNESS_SAVE_ROOT")
    if (
        env_seed is not None
        or env_ascension is not None
        or env_character is not None
        or env_save_root is not None
    ):
        setup = RunSetup(
            seed=env_seed.strip()
            if env_seed is not None and env_seed.strip()
            else setup.seed,
            seed_set=setup.seed_set,
            seed_policy=setup.seed_policy,
            ascension=_optional_int(env_ascension)
            if env_ascension is not None
            else setup.ascension,
            max_ascension=setup.max_ascension,
            character=env_character.strip().upper()
            if env_character is not None and env_character.strip()
            else setup.character,
            progress_file=setup.progress_file,
            save_root=env_save_root.strip()
            if env_save_root is not None and env_save_root.strip()
            else setup.save_root,
            increment_ascension_on_win=setup.increment_ascension_on_win,
            stop_after_consecutive_a10_wins=setup.stop_after_consecutive_a10_wins,
            stop_after_current_run=setup.stop_after_current_run,
        )

    progress = _load_json_file(setup.progress_file)
    current_ascension = progress.get("ascension")
    if isinstance(current_ascension, int):
        setup = RunSetup(
            seed=setup.seed,
            seed_set=setup.seed_set,
            seed_policy=setup.seed_policy,
            ascension=current_ascension,
            max_ascension=setup.max_ascension,
            character=setup.character,
            progress_file=setup.progress_file,
            save_root=setup.save_root,
            increment_ascension_on_win=setup.increment_ascension_on_win,
            stop_after_consecutive_a10_wins=setup.stop_after_consecutive_a10_wins,
            stop_after_current_run=setup.stop_after_current_run,
        )
    elif setup.ascension is not None:
        progress["ascension"] = setup.ascension
        _write_json_file(setup.progress_file, progress)

    seed = current_seed_for_setup(setup, progress)
    if seed != setup.seed:
        setup = RunSetup(
            seed=seed,
            seed_set=setup.seed_set,
            seed_policy=setup.seed_policy,
            ascension=setup.ascension,
            max_ascension=setup.max_ascension,
            character=setup.character,
            progress_file=setup.progress_file,
            save_root=setup.save_root,
            increment_ascension_on_win=setup.increment_ascension_on_win,
            stop_after_consecutive_a10_wins=setup.stop_after_consecutive_a10_wins,
            stop_after_current_run=setup.stop_after_current_run,
        )

    agent_config = config.get("agent")
    if not isinstance(agent_config, dict):
        agent_config = {}
    logging_config = config.get("logging")
    if not isinstance(logging_config, dict):
        logging_config = {}
    auto_config = config.get("auto_resolve")
    max_auto = config.get("max_auto_actions", 10)
    if isinstance(auto_config, dict):
        max_auto = auto_config.get("max_actions", max_auto)
        auto_config = auto_config.get("enabled", True)

    return HarnessConfig(
        run_setup=setup,
        agent=AgentConfig(
            agent_name=_optional_str(agent_config.get("agent_name")),
            model_name=_optional_str(agent_config.get("model_name")),
            condition_name=_optional_str(agent_config.get("condition_name")),
            prompt_version=_optional_str(agent_config.get("prompt_version")),
            memory_version=_optional_str(agent_config.get("memory_version")),
            memory_checksum=_optional_str(agent_config.get("memory_checksum")),
        ),
        logging=LoggingConfig(
            sqlite_path=_optional_str(logging_config.get("sqlite_path")),
            official_run_logging=bool(logging_config.get("official_run_logging")),
            harness_version=_optional_str(logging_config.get("harness_version")),
            git_commit=_optional_str(logging_config.get("git_commit")),
            memory_git_dir=_optional_str(logging_config.get("memory_git_dir")),
            memory_commit_on_run_start=bool(
                logging_config.get("memory_commit_on_run_start")
            ),
            memory_commit_paths=_string_tuple(
                logging_config.get("memory_commit_paths")
                or ["STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md"]
            ),
        ),
        auto_resolve=bool(auto_config) if auto_config is not None else True,
        max_auto_actions=int(max_auto),
    )


def load_run_setup(config_path: str) -> RunSetup:
    return load_harness_config(config_path).run_setup


def current_seed_for_setup(
    run_setup: RunSetup, progress: JsonDict | None = None
) -> str | None:
    if progress is None:
        progress = _load_json_file(run_setup.progress_file)
    if run_setup.seed_set:
        index = progress.get("current_seed_index")
        if not isinstance(index, int) or index < 0:
            index = 0
        return run_setup.seed_set[index % len(run_setup.seed_set)]
    return run_setup.seed


def _slug(value: Any) -> str:
    text = str(value).strip().lower()
    chars = [ch if ch.isalnum() else "_" for ch in text]
    return "_".join("".join(chars).split("_")).strip("_") or "action"


def _player(state: JsonDict) -> JsonDict:
    player = state.get("player")
    return player if isinstance(player, dict) else {}


def _player_potions(state: JsonDict) -> list[JsonDict]:
    potions = _player(state).get("potions")
    return potions if isinstance(potions, list) else []


def _potion_slots_full(state: JsonDict) -> bool:
    player = _player(state)
    max_slots = player.get("max_potion_slots")
    potions = _player_potions(state)
    return isinstance(max_slots, int) and max_slots > 0 and len(potions) >= max_slots


def _battle(state: JsonDict) -> JsonDict:
    battle = state.get("battle")
    return battle if isinstance(battle, dict) else {}


def _alive_enemies(state: JsonDict) -> list[JsonDict]:
    enemies = _battle(state).get("enemies")
    return enemies if isinstance(enemies, list) else []


def _current_energy(state: JsonDict) -> int | None:
    player = _player(state)
    battle = _battle(state)
    for source in (player, battle):
        for key in ("energy", "current_energy", "energy_current"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        energy = source.get("energy")
        if isinstance(energy, dict):
            for key in ("current", "amount"):
                value = energy.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    return value
    return None


def _playable_hand_cards(state: JsonDict) -> list[JsonDict]:
    hand = _player(state).get("hand")
    if not isinstance(hand, list):
        return []
    return [
        card
        for card in hand
        if isinstance(card, dict) and card.get("can_play") is True
    ]


def _combat_like(state_type: str) -> bool:
    return state_type in {"monster", "elite", "boss"}


def _wait_for_play_phase(client: Sts2Client, *, poll_interval: float = 0.5) -> JsonDict:
    """Poll game state until we're in a valid actionable state.

    During combat the game goes through animation/transition periods where
    is_play_phase is false. Any action submitted during those windows is
    either rejected or silently dropped. This helper waits until the game
    settles into a state where actions are meaningful.
    """
    while True:
        state = client.get_state(response_format="json")
        state_type = str(state.get("state_type") or "")

        # Non-combat states are always actionable.
        if not _combat_like(state_type):
            return state

        # Combat: only return when the player can actually act.
        battle = _battle(state)
        if battle.get("is_play_phase") is True and battle.get("turn") == "player":
            return state

        time.sleep(poll_interval)


def build_actions(state: JsonDict, run_setup: RunSetup | None = None) -> list[Action]:
    state_type = str(state.get("state_type") or "unknown")
    actions: list[Action] = []

    actions.extend(_menu_actions(state, run_setup))
    actions.extend(_global_potion_actions(state))

    if _combat_like(state_type):
        actions.extend(_combat_actions(state))
    elif state_type == "hand_select":
        actions.extend(_combat_selection_actions(state))
    elif state_type == "map":
        actions.extend(_map_actions(state))
    elif state_type == "event":
        actions.extend(_event_actions(state))
    elif state_type == "fake_merchant":
        actions.extend(_fake_merchant_actions(state))
    elif state_type == "shop":
        actions.extend(_shop_actions(state))
    elif state_type == "rest_site":
        actions.extend(_rest_site_actions(state))
    elif state_type == "rewards":
        actions.extend(_reward_actions(state))
    elif state_type == "card_reward":
        actions.extend(_card_reward_actions(state))
    elif state_type == "card_select":
        actions.extend(_card_select_actions(state))
    elif state_type == "choose_card":
        actions.extend(_choose_card_actions(state))
    elif state_type == "bundle_select":
        actions.extend(_bundle_actions(state))
    elif state_type == "relic_select":
        actions.extend(_relic_actions(state))
    elif state_type == "treasure":
        actions.extend(_treasure_actions(state))
    elif state_type == "crystal_sphere":
        actions.extend(_crystal_sphere_actions(state))
    elif state_type == "game_over":
        actions.append(
            Action(
                id="menu:main_menu",
                label="Return to main menu",
                category="menu",
                request={"action": "menu_select", "option": "main_menu"},
            )
        )

    return actions


def _menu_actions(state: JsonDict, run_setup: RunSetup | None = None) -> list[Action]:
    if state.get("state_type") != "menu":
        return []
    actions: list[Action] = []
    options = state.get("options")
    if not isinstance(options, list):
        return actions
    menu_screen = str(state.get("menu_screen") or "")
    selected_character = state.get("selected_character")
    has_selected_character = isinstance(selected_character, dict) and bool(
        selected_character.get("id")
    )
    for option in options:
        if isinstance(option, str):
            name = option
            enabled = True
        elif isinstance(option, dict):
            name = str(option.get("name") or "")
            enabled = option.get("enabled") is not False
        else:
            continue
        if not name or not enabled:
            continue

        if (
            run_setup is not None
            and menu_screen == "custom_run"
            and name.lower() in {"confirm", "embark"}
            and _load_json_file(run_setup.progress_file).get("stopped") is True
        ):
            continue

        if (
            run_setup is not None
            and menu_screen == "singleplayer"
            and (run_setup.seed or run_setup.ascension is not None)
            and name.lower() not in {"custom", "back"}
        ):
            continue

        if (
            run_setup is not None
            and menu_screen == "custom_run"
            and run_setup.character
            and not has_selected_character
            and name.upper() != run_setup.character
            and name.lower() not in {"back", "unready"}
        ):
            continue

        request: JsonDict = {"action": "menu_select", "option": name}
        notes: list[str] = []
        if (
            run_setup is not None
            and menu_screen == "custom_run"
            and name.lower() in {"confirm", "embark"}
        ):
            if run_setup.seed:
                request["seed"] = run_setup.seed
                notes.append("seed supplied by harness config")
            if run_setup.ascension is not None:
                request["ascension"] = run_setup.ascension
                notes.append("ascension supplied by harness config")

        actions.append(
            Action(
                id=f"menu:{_slug(name)}",
                label=f"Select menu option: {name}",
                category="menu",
                request=request,
                notes=tuple(notes),
            )
        )
    return actions


def _global_potion_actions(state: JsonDict) -> list[Action]:
    actions: list[Action] = []
    potions = _player_potions(state)
    if not potions:
        return actions

    state_type = str(state.get("state_type") or "")
    in_play_phase = _battle(state).get("is_play_phase") is True
    can_use_context = _combat_like(state_type) and in_play_phase

    for potion in potions:
        slot = potion.get("slot")
        if not isinstance(slot, int):
            continue
        potion_name = _name(potion, "name", "id", fallback=f"potion {slot}")
        target_type = str(potion.get("target_type") or "")

        if can_use_context:
            if target_type == "AnyEnemy":
                for enemy in _alive_enemies(state):
                    entity_id = enemy.get("entity_id")
                    if not entity_id:
                        continue
                    enemy_name = _name(enemy, "name", "entity_id")
                    actions.append(
                        Action(
                            id=f"use_potion:{slot}:{_slug(entity_id)}",
                            label=f"Use potion[{slot}] {potion_name} on {enemy_name}",
                            category="potion",
                            request={
                                "action": "use_potion",
                                "slot": slot,
                                "target": entity_id,
                            },
                        )
                    )
            else:
                actions.append(
                    Action(
                        id=f"use_potion:{slot}",
                        label=f"Use potion[{slot}] {potion_name}",
                        category="potion",
                        request={"action": "use_potion", "slot": slot},
                    )
                )

        actions.append(
            Action(
                id=f"discard_potion:{slot}",
                label=f"Discard potion[{slot}] {potion_name}",
                category="potion",
                request={"action": "discard_potion", "slot": slot},
            )
        )

    return actions


def _combat_actions(state: JsonDict) -> list[Action]:
    battle = _battle(state)
    actions: list[Action] = []

    if battle.get("is_play_phase") is not True or battle.get("turn") != "player":
        return actions

    enemies = _alive_enemies(state)
    playable_cards = _playable_hand_cards(state)
    for card in playable_cards:
        index = card.get("index")
        if not isinstance(index, int):
            continue
        card_name = _name(card, "name", "id", fallback=f"card {index}")
        target_type = str(card.get("target_type") or "")
        if target_type == "AnyEnemy":
            for enemy in enemies:
                entity_id = enemy.get("entity_id")
                if not entity_id:
                    continue
                enemy_name = _name(enemy, "name", "entity_id")
                actions.append(
                    Action(
                        id=f"play_card:{index}:{_slug(entity_id)}",
                        label=f"Play hand[{index}] {card_name} on {enemy_name}",
                        category="combat",
                        request={
                            "action": "play_card",
                            "card_index": index,
                            "target": entity_id,
                        },
                    )
                )
        else:
            actions.append(
                Action(
                    id=f"play_card:{index}",
                    label=f"Play hand[{index}] {card_name}",
                    category="combat",
                    request={"action": "play_card", "card_index": index},
                )
            )

    end_turn_id = "end_turn"
    notes: tuple[str, ...] = ()
    energy = _current_energy(state)
    if energy is not None and energy > 0 and playable_cards:
        end_turn_id = "end_turn_confirm"
        notes = (
            f"Confirmation required: {energy} energy and "
            f"{len(playable_cards)} playable card(s) remain.",
            "Use this only when intentionally ending the turn.",
        )
    actions.append(
        Action(
            id=end_turn_id,
            label=(
                "Confirm end turn"
                if end_turn_id == "end_turn_confirm"
                else "End turn"
            ),
            category="combat",
            request={"action": "end_turn"},
            notes=notes,
        )
    )
    return actions


def _combat_selection_actions(state: JsonDict) -> list[Action]:
    hand_select = state.get("hand_select")
    if not isinstance(hand_select, dict):
        return []
    actions: list[Action] = []
    for card in hand_select.get("cards") or []:
        if not isinstance(card, dict):
            continue
        index = card.get("index")
        if not isinstance(index, int):
            continue
        card_name = _name(card, "name", "id", fallback=f"card {index}")
        actions.append(
            Action(
                id=f"combat_select_card:{index}",
                label=f"Select hand card[{index}] {card_name}",
                category="combat_selection",
                request={"action": "combat_select_card", "card_index": index},
            )
        )
    if hand_select.get("can_confirm") is True:
        actions.append(
            Action(
                id="combat_confirm_selection",
                label="Confirm combat card selection",
                category="combat_selection",
                request={"action": "combat_confirm_selection"},
            )
        )
    return actions


def _map_actions(state: JsonDict) -> list[Action]:
    map_state = state.get("map")
    if not isinstance(map_state, dict):
        return []
    actions: list[Action] = []
    for node in map_state.get("next_options") or []:
        if not isinstance(node, dict):
            continue
        index = node.get("index")
        if not isinstance(index, int):
            continue
        room_type = node.get("type") or "unknown"
        col = node.get("col")
        row = node.get("row")
        actions.append(
            Action(
                id=f"map_choose_node:{index}",
                label=f"Travel to {room_type} node[{index}] at ({col},{row})",
                category="map",
                request={"action": "choose_map_node", "index": index},
            )
        )
    return actions


def _event_actions(state: JsonDict) -> list[Action]:
    event = state.get("event")
    if not isinstance(event, dict):
        return []
    actions: list[Action] = []
    if event.get("in_dialogue") is True:
        actions.append(
            Action(
                id="event_advance_dialogue",
                label="Advance event dialogue",
                category="event",
                request={"action": "advance_dialogue"},
            )
        )
        return actions
    for option in event.get("options") or []:
        if not isinstance(option, dict) or option.get("is_locked") is True:
            continue
        index = option.get("index")
        if not isinstance(index, int):
            continue
        title = _name(option, "title", "description", fallback=f"option {index}")
        actions.append(
            Action(
                id=f"event_choose_option:{index}",
                label=f"Choose event option[{index}]: {title}",
                category="event",
                request={"action": "choose_event_option", "index": index},
            )
        )
    return actions


def _fake_merchant_actions(state: JsonDict) -> list[Action]:
    fake = state.get("fake_merchant")
    if not isinstance(fake, dict):
        return []
    actions = _shop_actions(
        {"state_type": "shop", "shop": fake.get("shop"), "player": state.get("player")}
    )
    shop = fake.get("shop")
    if isinstance(shop, dict) and shop.get("can_proceed") is True:
        actions.append(
            Action(
                id="proceed_to_map",
                label="Proceed to map",
                category="proceed",
                request={"action": "proceed"},
            )
        )
    return actions


def _shop_actions(state: JsonDict) -> list[Action]:
    shop = state.get("shop")
    if not isinstance(shop, dict):
        return []
    actions: list[Action] = []
    potion_full = _potion_slots_full(state)
    for item in shop.get("items") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        if item.get("is_stocked") is False or item.get("can_afford") is False:
            continue
        category = str(item.get("category") or "item")
        if category == "potion" and potion_full:
            continue
        label = _shop_item_label(item, index)
        actions.append(
            Action(
                id=f"shop_purchase:{index}",
                label=label,
                category="shop",
                request={"action": "shop_purchase", "index": index},
            )
        )
    # STS2MCP's proceed action closes an open shop inventory first, then clicks
    # the room proceed button. The raw state can report can_proceed=false while
    # the inventory is open, but {"action": "proceed"} is still the correct and
    # valid way to leave the shop.
    actions.append(
        Action(
            id="proceed_to_map",
            label="Leave shop and proceed to map",
            category="proceed",
            request={"action": "proceed"},
        )
    )
    return actions


def _shop_item_label(item: JsonDict, index: int) -> str:
    category = str(item.get("category") or "item")
    price = item.get("price")
    if category == "card":
        name = _name(item, "card_name", "card_id", fallback=f"card {index}")
    elif category == "relic":
        name = _name(item, "relic_name", "relic_id", fallback=f"relic {index}")
    elif category == "potion":
        name = _name(item, "potion_name", "potion_id", fallback=f"potion {index}")
    elif category == "card_removal":
        name = "card removal"
    else:
        name = category
    return f"Buy shop item[{index}] {name} for {price} gold"


def _rest_site_actions(state: JsonDict) -> list[Action]:
    rest = state.get("rest_site")
    if not isinstance(rest, dict):
        return []
    actions: list[Action] = []
    for option in rest.get("options") or []:
        if not isinstance(option, dict) or option.get("is_enabled") is False:
            continue
        index = option.get("index")
        if not isinstance(index, int):
            continue
        name = _name(option, "name", "id", fallback=f"option {index}")
        actions.append(
            Action(
                id=f"rest_choose_option:{index}",
                label=f"Choose rest option[{index}]: {name}",
                category="rest_site",
                request={"action": "choose_rest_option", "index": index},
            )
        )
    if rest.get("can_proceed") is True:
        actions.append(
            Action(
                id="proceed_to_map",
                label="Proceed to map",
                category="proceed",
                request={"action": "proceed"},
            )
        )
    return actions


def _reward_actions(state: JsonDict) -> list[Action]:
    rewards = state.get("rewards")
    if not isinstance(rewards, dict):
        return []
    actions: list[Action] = []
    potion_full = _potion_slots_full(state)
    for item in rewards.get("items") or []:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        reward_type = str(item.get("type") or "reward")
        label = _reward_label(item, index)
        if reward_type == "potion" and potion_full:
            actions.append(
                Action(
                    id=f"rewards_claim:{index}:disabled",
                    label=label,
                    category="rewards",
                    request={"action": "claim_reward", "index": index},
                    notes=(
                        "Disabled: potion slots are full. Discard a potion before claiming this reward.",
                    ),
                    enabled=False,
                )
            )
            continue
        actions.append(
            Action(
                id=f"rewards_claim:{index}",
                label=label,
                category="rewards",
                request={"action": "claim_reward", "index": index},
            )
        )
    if rewards.get("can_proceed") is True:
        actions.append(
            Action(
                id="proceed_to_map",
                label="Proceed to map",
                category="proceed",
                request={"action": "proceed"},
            )
        )
    return actions


def _reward_label(item: JsonDict, index: int) -> str:
    reward_type = str(item.get("type") or "reward")
    if reward_type == "gold":
        detail = f"{item.get('gold_amount')} gold"
    elif reward_type == "potion":
        detail = _name(item, "potion_name", "potion_id", fallback="potion")
    else:
        detail = str(item.get("description") or reward_type)
    return f"Claim reward[{index}]: {detail}"


def _card_reward_actions(state: JsonDict) -> list[Action]:
    card_reward = state.get("card_reward")
    if not isinstance(card_reward, dict):
        return []
    actions: list[Action] = []
    for card in card_reward.get("cards") or []:
        if not isinstance(card, dict):
            continue
        index = card.get("index")
        if not isinstance(index, int):
            continue
        card_name = _name(card, "name", "id", fallback=f"card {index}")
        actions.append(
            Action(
                id=f"rewards_pick_card:{index}",
                label=f"Pick card reward[{index}] {card_name}",
                category="card_reward",
                request={"action": "select_card_reward", "card_index": index},
            )
        )
    if card_reward.get("can_skip") is True:
        actions.append(
            Action(
                id="rewards_skip_card",
                label="Skip card reward",
                category="card_reward",
                request={"action": "skip_card_reward"},
            )
        )
    return actions


def _card_select_actions(state: JsonDict) -> list[Action]:
    selection = state.get("card_select")
    if not isinstance(selection, dict):
        return []
    actions: list[Action] = []
    screen_type = str(selection.get("screen_type") or "select")
    if selection.get("preview_showing") is not True:
        for card in selection.get("cards") or []:
            if not isinstance(card, dict):
                continue
            index = card.get("index")
            if not isinstance(index, int):
                continue
            card_name = _name(card, "name", "id", fallback=f"card {index}")
            actions.append(
                Action(
                    id=f"deck_select_card:{index}",
                    label=f"Select {screen_type} card[{index}] {card_name}",
                    category="card_select",
                    request={"action": "select_card", "index": index},
                )
            )
    if selection.get("can_confirm") is True:
        actions.append(
            Action(
                id="deck_confirm_selection",
                label=f"Confirm {screen_type} selection",
                category="card_select",
                request={"action": "confirm_selection"},
            )
        )
    if selection.get("can_cancel") is True:
        actions.append(
            Action(
                id="deck_cancel_selection",
                label=f"Cancel {screen_type} selection",
                category="card_select",
                request={"action": "cancel_selection"},
            )
        )
    return actions


def _choose_card_actions(state: JsonDict) -> list[Action]:
    choose = state.get("choose_card")
    if not isinstance(choose, dict):
        return []
    actions: list[Action] = []
    for card in choose.get("cards") or []:
        if not isinstance(card, dict):
            continue
        index = card.get("index")
        if not isinstance(index, int):
            continue
        card_name = _name(card, "name", "id", fallback=f"card {index}")
        actions.append(
            Action(
                id=f"deck_select_card:{index}",
                label=f"Choose card[{index}] {card_name}",
                category="choose_card",
                request={"action": "select_card", "index": index},
            )
        )
    if choose.get("can_skip") is True or choose.get("can_cancel") is True:
        actions.append(
            Action(
                id="deck_cancel_selection",
                label="Skip/cancel card choice",
                category="choose_card",
                request={"action": "cancel_selection"},
            )
        )
    return actions


def _bundle_actions(state: JsonDict) -> list[Action]:
    bundle = state.get("bundle_select")
    if not isinstance(bundle, dict):
        return []
    actions: list[Action] = []
    if bundle.get("preview_showing") is not True:
        for item in bundle.get("bundles") or []:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if not isinstance(index, int):
                continue
            count = item.get("card_count")
            actions.append(
                Action(
                    id=f"bundle_select:{index}",
                    label=f"Preview bundle[{index}] ({count} cards)",
                    category="bundle_select",
                    request={"action": "select_bundle", "index": index},
                )
            )
    if bundle.get("can_confirm") is True:
        actions.append(
            Action(
                id="bundle_confirm_selection",
                label="Confirm bundle selection",
                category="bundle_select",
                request={"action": "confirm_bundle_selection"},
            )
        )
    if bundle.get("can_cancel") is True:
        actions.append(
            Action(
                id="bundle_cancel_selection",
                label="Cancel bundle selection",
                category="bundle_select",
                request={"action": "cancel_bundle_selection"},
            )
        )
    return actions


def _relic_actions(state: JsonDict) -> list[Action]:
    relic_select = state.get("relic_select")
    if not isinstance(relic_select, dict):
        return []
    actions: list[Action] = []
    for relic in relic_select.get("relics") or []:
        if not isinstance(relic, dict):
            continue
        index = relic.get("index")
        if not isinstance(index, int):
            continue
        relic_name = _name(relic, "name", "id", fallback=f"relic {index}")
        actions.append(
            Action(
                id=f"relic_select:{index}",
                label=f"Select relic[{index}] {relic_name}",
                category="relic_select",
                request={"action": "select_relic", "index": index},
            )
        )
    if relic_select.get("can_skip") is True:
        actions.append(
            Action(
                id="relic_skip",
                label="Skip relic selection",
                category="relic_select",
                request={"action": "skip_relic_selection"},
            )
        )
    return actions


def _treasure_actions(state: JsonDict) -> list[Action]:
    treasure = state.get("treasure")
    if not isinstance(treasure, dict):
        return []
    actions: list[Action] = []
    for relic in treasure.get("relics") or []:
        if not isinstance(relic, dict):
            continue
        index = relic.get("index")
        if not isinstance(index, int):
            continue
        relic_name = _name(relic, "name", "id", fallback=f"relic {index}")
        actions.append(
            Action(
                id=f"treasure_claim_relic:{index}",
                label=f"Claim treasure relic[{index}] {relic_name}",
                category="treasure",
                request={"action": "claim_treasure_relic", "index": index},
            )
        )
    if treasure.get("can_proceed") is True:
        actions.append(
            Action(
                id="proceed_to_map",
                label="Proceed to map",
                category="proceed",
                request={"action": "proceed"},
            )
        )
    return actions


def _crystal_sphere_actions(state: JsonDict) -> list[Action]:
    sphere = state.get("crystal_sphere")
    if not isinstance(sphere, dict):
        return []
    actions: list[Action] = []
    if sphere.get("can_use_big_tool") is True:
        actions.append(
            Action(
                id="crystal_sphere_set_tool:big",
                label="Use big divination tool",
                category="crystal_sphere",
                request={"action": "crystal_sphere_set_tool", "tool": "big"},
            )
        )
    if sphere.get("can_use_small_tool") is True:
        actions.append(
            Action(
                id="crystal_sphere_set_tool:small",
                label="Use small divination tool",
                category="crystal_sphere",
                request={"action": "crystal_sphere_set_tool", "tool": "small"},
            )
        )
    for cell in sphere.get("clickable_cells") or []:
        if not isinstance(cell, dict):
            continue
        x = cell.get("x")
        y = cell.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            continue
        actions.append(
            Action(
                id=f"crystal_sphere_click_cell:{x}:{y}",
                label=f"Reveal Crystal Sphere cell ({x},{y})",
                category="crystal_sphere",
                request={"action": "crystal_sphere_click_cell", "x": x, "y": y},
            )
        )
    if sphere.get("can_proceed") is True:
        actions.append(
            Action(
                id="crystal_sphere_proceed",
                label="Proceed from Crystal Sphere",
                category="crystal_sphere",
                request={"action": "crystal_sphere_proceed"},
            )
        )
    return actions


def action_dicts(actions: list[Action]) -> list[JsonDict]:
    return [action.as_dict(index) for index, action in enumerate(actions)]


def find_action(actions: list[Action], action_ref: str) -> Action:
    if action_ref.isdigit():
        index = int(action_ref)
        if 0 <= index < len(actions) and actions[index].enabled:
            return actions[index]
        if 0 <= index < len(actions):
            raise ValueError(f"action index {index} is currently disabled")
        raise ValueError(f"action index {index} is not currently legal")

    matches = [action for action in actions if action.id == action_ref and action.enabled]
    if len(matches) == 1:
        return matches[0]
    disabled_matches = [action for action in actions if action.id == action_ref]
    if disabled_matches:
        raise ValueError(f"action_id {action_ref!r} is currently disabled")
    if not matches:
        raise ValueError(f"action_id {action_ref!r} is not currently legal")
    raise ValueError(
        f"action_id {action_ref!r} is ambiguous; use one of: "
        + ", ".join(action.id for action in matches)
    )


def _extract_run_seed(run: JsonDict) -> str | None:
    rng = run.get("rng")
    if isinstance(rng, dict) and rng.get("seed") is not None:
        return str(rng.get("seed"))
    for key in ("seed", "run_seed"):
        if run.get(key) is not None:
            return str(run.get(key))
    return None


def _extract_run_ascension(run: JsonDict) -> int | None:
    for key in ("ascension", "ascension_level", "ascensionLevel"):
        value = run.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _extract_run_victory(run: JsonDict) -> bool:
    for key in ("victory", "win", "is_victory"):
        if run.get(key) is True:
            return True
    return False


def _run_identity(run: JsonDict) -> str:
    for key in ("run_id", "id", "start_time"):
        value = run.get(key)
        if value is not None:
            return str(value)
    path = run.get("_path")
    if path is not None:
        return str(path)
    payload = json.dumps(run, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _history_run_paths(save_root: str) -> list[str]:
    root = os.path.expanduser(save_root)
    matches: list[tuple[float, str]] = []
    for dirpath, _, filenames in os.walk(root):
        normalized = dirpath.replace(os.sep, "/").lower()
        if "/saves/history" not in normalized and not normalized.endswith("/history"):
            continue
        for filename in filenames:
            if not filename.endswith(".run"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                matches.append((os.path.getmtime(path), path))
            except OSError:
                continue
    matches.sort(reverse=True)
    return [path for _, path in matches]


def read_latest_history_run(save_root: str) -> JsonDict:
    for path in _history_run_paths(save_root):
        try:
            data = _load_json_file(path)
        except json.JSONDecodeError:
            continue
        if data:
            data["_path"] = path
            data["_mtime"] = os.path.getmtime(path)
            return data
    raise FileNotFoundError(
        f"No .run history file found under {os.path.expanduser(save_root)}"
    )


def _advance_seed_index(progress: JsonDict, run_setup: RunSetup) -> None:
    if not run_setup.seed_set:
        return
    current = progress.get("current_seed_index")
    if not isinstance(current, int) or current < 0:
        current = 0
    progress["current_seed_index"] = (current + 1) % len(run_setup.seed_set)


def maybe_update_progress_after_state(
    client: Sts2Client, state: JsonDict, run_setup: RunSetup
) -> JsonDict | None:
    del client
    if state.get("state_type") != "game_over":
        return None
    if not run_setup.increment_ascension_on_win and not run_setup.stop_after_current_run:
        return None

    try:
        latest = read_latest_history_run(run_setup.save_root)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None

    run_id = _run_identity(latest)
    progress = _load_json_file(run_setup.progress_file)
    if progress.get("last_processed_history_run_id") == run_id:
        return None

    victory = _extract_run_victory(latest)
    completed_ascension = _extract_run_ascension(latest)
    completed_seed = _extract_run_seed(latest)

    current_ascension = progress.get("ascension")
    if not isinstance(current_ascension, int):
        current_ascension = (
            run_setup.ascension if run_setup.ascension is not None else 0
        )

    next_ascension = current_ascension
    previous_seed_index = progress.get("current_seed_index", 0)
    consecutive_a10 = progress.get("consecutive_a10_wins")
    if not isinstance(consecutive_a10, int):
        consecutive_a10 = 0

    if run_setup.increment_ascension_on_win:
        if victory:
            won_at_max = (
                completed_ascension == run_setup.max_ascension
                or current_ascension >= run_setup.max_ascension
            )
            if won_at_max:
                consecutive_a10 += 1
                _advance_seed_index(progress, run_setup)
            else:
                consecutive_a10 = 0
                next_ascension = min(current_ascension + 1, run_setup.max_ascension)
                if run_setup.seed_policy in {"fixed_list", "fixed_list_until_win"}:
                    _advance_seed_index(progress, run_setup)
        else:
            consecutive_a10 = 0
            if run_setup.seed_policy == "fixed_list":
                _advance_seed_index(progress, run_setup)

    progress["ascension"] = next_ascension
    progress["consecutive_a10_wins"] = consecutive_a10
    progress["last_processed_history_run_id"] = run_id
    progress["last_processed_history_path"] = latest.get("_path")
    progress["last_completed_seed"] = completed_seed
    progress["last_completed_ascension"] = completed_ascension
    progress["last_completed_victory"] = victory
    if consecutive_a10 >= run_setup.stop_after_consecutive_a10_wins:
        progress["stopped"] = True
        progress["stop_reason"] = (
            f"{consecutive_a10} consecutive A{run_setup.max_ascension} wins"
        )
    if run_setup.stop_after_current_run:
        progress["stopped"] = True
        progress["stop_reason"] = "stop_after_current_run"
    _write_json_file(run_setup.progress_file, progress)

    return {
        "status": "updated",
        "reason": "completed_run_detected",
        "run_id": run_id,
        "seed": completed_seed,
        "victory": victory,
        "previous_ascension": current_ascension,
        "next_ascension": next_ascension,
        "previous_seed_index": previous_seed_index,
        "next_seed_index": progress.get("current_seed_index", previous_seed_index),
        "consecutive_a10_wins": consecutive_a10,
        "stopped": progress.get("stopped", False),
        "progress_file": run_setup.progress_file,
    }


def _current_run_save_paths(save_root: str) -> list[str]:
    root = os.path.expanduser(save_root)
    matches: list[tuple[float, str]] = []
    for dirpath, _, filenames in os.walk(root):
        if "current_run.save" not in filenames:
            continue
        path = os.path.join(dirpath, "current_run.save")
        try:
            matches.append((os.path.getmtime(path), path))
        except OSError:
            continue
    matches.sort(reverse=True)
    return [path for _, path in matches]


def read_latest_current_run(save_root: str) -> JsonDict:
    for path in _current_run_save_paths(save_root):
        try:
            data = _load_json_file(path)
        except json.JSONDecodeError:
            continue
        if data:
            data["_path"] = path
            return data
    raise FileNotFoundError(
        f"No current_run.save found under {os.path.expanduser(save_root)}"
    )


def _verify_current_run_save(
    current_run: JsonDict, expected_seed: Any, expected_ascension: Any
) -> JsonDict:
    rng = current_run.get("rng")
    actual_seed = rng.get("seed") if isinstance(rng, dict) else None
    actual_ascension = current_run.get("ascension")
    seed_ok = expected_seed is None or str(actual_seed) == str(expected_seed)
    ascension_ok = expected_ascension is None or actual_ascension == expected_ascension
    return {
        "status": "ok" if seed_ok and ascension_ok else "mismatch",
        "save_path": current_run.get("_path"),
        "expected": {
            "seed": expected_seed,
            "ascension": expected_ascension,
        },
        "actual": {
            "seed": actual_seed,
            "ascension": actual_ascension,
            "game_mode": current_run.get("game_mode"),
            "start_time": current_run.get("start_time"),
        },
    }


def verify_started_run_setup(
    action: Action, run_setup: RunSetup, *, timeout: float = 5.0
) -> JsonDict | None:
    request = action.request
    if request.get("action") != "menu_select" or str(
        request.get("option") or ""
    ).lower() not in {"confirm", "embark"}:
        return None
    if "seed" not in request and "ascension" not in request:
        return None

    deadline = time.monotonic() + timeout
    last_error: str | None = None
    last_verification: JsonDict | None = None
    while True:
        try:
            expected_seed = request.get("seed")
            expected_ascension = request.get("ascension")
            verifications = []
            for path in _current_run_save_paths(run_setup.save_root):
                current_run = _load_json_file(path)
                if not current_run:
                    continue
                current_run["_path"] = path
                verification = _verify_current_run_save(
                    current_run, expected_seed, expected_ascension
                )
                if verification["status"] == "ok":
                    return verification
                verifications.append(verification)
            if verifications:
                last_verification = verifications[0]
            if time.monotonic() >= deadline and last_verification is not None:
                return last_verification
            time.sleep(0.25)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                if last_verification is not None:
                    return last_verification
                return {
                    "status": "error",
                    "error": last_error,
                    "save_root": os.path.expanduser(run_setup.save_root),
                }
            time.sleep(0.25)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _logging_enabled(config: HarnessConfig) -> bool:
    return (
        config.logging.official_run_logging
        and bool(config.logging.sqlite_path)
        and bool(config.agent.agent_name)
        and bool(config.agent.model_name)
        and bool(config.agent.condition_name)
        and bool(config.run_setup.seed)
        and config.run_setup.ascension is not None
    )


def _connect_log_db(config: HarnessConfig) -> sqlite3.Connection | None:
    if not _logging_enabled(config) or config.logging.sqlite_path is None:
        return None
    path = os.path.expanduser(config.logging.sqlite_path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    _init_log_db(conn)
    return conn


def _init_log_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          agent_name TEXT NOT NULL,
          model_name TEXT NOT NULL,
          condition_name TEXT NOT NULL,
          seed TEXT NOT NULL,
          ascension INTEGER,
          start_time TEXT NOT NULL,
          end_time TEXT,
          final_floor INTEGER,
          act INTEGER,
          boss_reached INTEGER,
          victory INTEGER,
          death_reason TEXT,
          character TEXT,
          game_mode TEXT,
          custom_settings TEXT,
          harness_version TEXT,
          prompt_version TEXT,
          memory_version TEXT,
          memory_checksum TEXT,
          git_commit TEXT,
          total_model_calls INTEGER,
          total_input_tokens INTEGER,
          total_output_tokens INTEGER,
          total_tool_calls INTEGER,
          invalid_action_count INTEGER DEFAULT 0,
          harness_observations INTEGER DEFAULT 0,
          agent_actions INTEGER DEFAULT 0,
          auto_actions INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS steps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          step_index INTEGER NOT NULL,
          floor INTEGER,
          room_type TEXT,
          state_type TEXT,
          hp INTEGER,
          max_hp INTEGER,
          gold INTEGER,
          deck_size INTEGER,
          relic_count INTEGER,
          potion_count INTEGER,
          legal_actions TEXT,
          action_chosen TEXT,
          action_source TEXT NOT NULL,
          observation_hash TEXT,
          prompt_hash TEXT,
          response_hash TEXT,
          tool_calls TEXT,
          timestamp TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_summaries (
          run_id TEXT PRIMARY KEY,
          agent_summary TEXT,
          harness_summary TEXT,
          final_memory_diff TEXT,
          notable_mistakes TEXT,
          notable_successes TEXT,
          timestamp TEXT NOT NULL
        );
        """
    )
    conn.commit()


def _run_id(config: HarnessConfig, verification: JsonDict | None = None) -> str:
    seed = config.run_setup.seed or "unknown_seed"
    ascension = config.run_setup.ascension
    start_time = None
    if verification:
        actual = verification.get("actual")
        if isinstance(actual, dict):
            start_time = actual.get("start_time")
    if start_time is None:
        start_time = int(time.time())
    parts = [
        config.agent.agent_name or "agent",
        config.agent.condition_name or "condition",
        seed,
        f"a{ascension if ascension is not None else 'x'}",
        str(start_time),
    ]
    return ":".join(_slug(part) for part in parts)


def _state_floor(state: JsonDict) -> int | None:
    run = state.get("run")
    if not isinstance(run, dict):
        run = {}
    for source in (state, run, _player(state)):
        for key in ("floor", "floor_num", "floorNum"):
            value = source.get(key)
            if isinstance(value, int):
                return value
    return None


def _state_act(state: JsonDict) -> int | None:
    run = state.get("run")
    if not isinstance(run, dict):
        run = {}
    for source in (state, run):
        for key in ("act", "act_num", "actNum"):
            value = source.get(key)
            if isinstance(value, int):
                return value
    return None


def _state_room_type(state: JsonDict) -> str | None:
    value = state.get("room_type") or state.get("room")
    if value is None:
        map_state = state.get("map")
        if isinstance(map_state, dict):
            value = map_state.get("current_room_type")
    return str(value) if value is not None else None


def _deck_size(state: JsonDict) -> int | None:
    player = _player(state)
    for key in ("deck_size", "master_deck_size"):
        value = player.get(key)
        if isinstance(value, int):
            return value
    deck = player.get("deck") or player.get("master_deck")
    return len(deck) if isinstance(deck, list) else None


def _state_counts(state: JsonDict) -> tuple[int | None, int | None]:
    player = _player(state)
    relics = player.get("relics")
    potions = player.get("potions")
    return (
        len(relics) if isinstance(relics, list) else None,
        len(potions) if isinstance(potions, list) else None,
    )


def _progress_current_run_id(config: HarnessConfig) -> str | None:
    progress = _load_json_file(config.run_setup.progress_file)
    value = progress.get("current_run_id")
    return str(value) if value else None


def _set_progress_current_run_id(config: HarnessConfig, run_id: str) -> None:
    progress = _load_json_file(config.run_setup.progress_file)
    progress["current_run_id"] = run_id
    _write_json_file(config.run_setup.progress_file, progress)


def commit_memory_snapshot(config: HarnessConfig, run_id: str) -> JsonDict | None:
    if not config.logging.memory_commit_on_run_start:
        return None
    if not config.logging.memory_git_dir:
        return {"status": "skipped", "reason": "memory_git_dir_not_configured"}

    git_dir = os.path.expanduser(config.logging.memory_git_dir)
    paths = config.logging.memory_commit_paths
    if not paths:
        return {"status": "skipped", "reason": "no_memory_commit_paths"}

    def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=git_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    add_result = run_git(["add", "-f", "--", *paths])
    if add_result.returncode != 0:
        return {
            "status": "error",
            "reason": "git_add_failed",
            "stderr": add_result.stderr.strip(),
        }

    diff_result = run_git(["diff", "--cached", "--quiet", "--", *paths])
    if diff_result.returncode == 0:
        return {"status": "unchanged"}
    if diff_result.returncode != 1:
        return {
            "status": "error",
            "reason": "git_diff_failed",
            "stderr": diff_result.stderr.strip(),
        }

    message = f"memory snapshot at run start {run_id}"
    commit_result = run_git(["commit", "-m", message, "--", *paths])
    if commit_result.returncode != 0:
        return {
            "status": "error",
            "reason": "git_commit_failed",
            "stderr": commit_result.stderr.strip(),
        }

    rev_result = run_git(["rev-parse", "--short", "HEAD"])
    return {
        "status": "committed",
        "commit": rev_result.stdout.strip() if rev_result.returncode == 0 else None,
        "message": message,
    }


def start_logged_run(
    config: HarnessConfig, state: JsonDict, verification: JsonDict | None
) -> JsonDict | None:
    conn = _connect_log_db(config)
    if conn is None:
        return None
    run_id = _run_id(config, verification)
    player = _player(state)
    conn.execute(
        """
        INSERT OR IGNORE INTO runs (
          run_id, agent_name, model_name, condition_name, seed, ascension,
          start_time, character, game_mode, custom_settings, harness_version,
          prompt_version, memory_version, memory_checksum, git_commit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            config.agent.agent_name,
            config.agent.model_name,
            config.agent.condition_name,
            config.run_setup.seed,
            config.run_setup.ascension,
            _now_utc(),
            config.run_setup.character
            or player.get("character_id")
            or player.get("character"),
            state.get("game_mode"),
            json.dumps({"run_setup_verification": verification}, sort_keys=True),
            config.logging.harness_version,
            config.agent.prompt_version,
            config.agent.memory_version,
            config.agent.memory_checksum,
            config.logging.git_commit,
        ),
    )
    conn.commit()
    conn.close()
    _set_progress_current_run_id(config, run_id)
    result: JsonDict = {"status": "started", "run_id": run_id}
    memory_commit = commit_memory_snapshot(config, run_id)
    if memory_commit is not None:
        result["memory_commit"] = memory_commit
    return result


def log_step(
    config: HarnessConfig,
    state: JsonDict,
    actions: list[Action],
    action: Action,
    *,
    action_source: str,
) -> None:
    conn = _connect_log_db(config)
    run_id = _progress_current_run_id(config)
    if conn is None or run_id is None:
        if conn is not None:
            conn.close()
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    step_index = int(row[0])
    player = _player(state)
    relic_count, potion_count = _state_counts(state)
    legal_actions = action_dicts(actions)
    observation_json = json.dumps(
        {"state": state, "actions": legal_actions}, sort_keys=True, default=str
    )
    conn.execute(
        """
        INSERT INTO steps (
          run_id, step_index, floor, room_type, state_type, hp, max_hp, gold,
          deck_size, relic_count, potion_count, legal_actions, action_chosen,
          action_source, observation_hash, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            step_index,
            _state_floor(state),
            _state_room_type(state),
            state.get("state_type"),
            player.get("hp"),
            player.get("max_hp"),
            player.get("gold"),
            _deck_size(state),
            relic_count,
            potion_count,
            json.dumps(legal_actions, sort_keys=True),
            json.dumps(action.as_dict(), sort_keys=True),
            action_source,
            hashlib.sha256(observation_json.encode("utf-8")).hexdigest(),
            _now_utc(),
        ),
    )
    count_column = "auto_actions" if action_source == "auto" else "agent_actions"
    conn.execute(
        f"""
        UPDATE runs
        SET harness_observations = COALESCE(harness_observations, 0) + 1,
            {count_column} = COALESCE({count_column}, 0) + 1
        WHERE run_id = ?
        """,
        (run_id,),
    )
    conn.commit()
    conn.close()


def log_invalid_action(
    config: HarnessConfig,
    state: JsonDict,
    actions: list[Action],
    action_ref: str,
    error: str,
) -> None:
    conn = _connect_log_db(config)
    run_id = _progress_current_run_id(config)
    if conn is None or run_id is None:
        if conn is not None:
            conn.close()
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(step_index), -1) + 1 FROM steps WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    step_index = int(row[0])
    player = _player(state)
    relic_count, potion_count = _state_counts(state)
    legal_actions = action_dicts(actions)
    observation_json = json.dumps(
        {"state": state, "actions": legal_actions}, sort_keys=True, default=str
    )
    chosen = {
        "id": action_ref,
        "status": "invalid",
        "error": error,
    }
    conn.execute(
        """
        INSERT INTO steps (
          run_id, step_index, floor, room_type, state_type, hp, max_hp, gold,
          deck_size, relic_count, potion_count, legal_actions, action_chosen,
          action_source, observation_hash, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            step_index,
            _state_floor(state),
            _state_room_type(state),
            state.get("state_type"),
            player.get("hp"),
            player.get("max_hp"),
            player.get("gold"),
            _deck_size(state),
            relic_count,
            potion_count,
            json.dumps(legal_actions, sort_keys=True),
            json.dumps(chosen, sort_keys=True),
            "invalid_agent",
            hashlib.sha256(observation_json.encode("utf-8")).hexdigest(),
            _now_utc(),
        ),
    )
    conn.execute(
        """
        UPDATE runs
        SET harness_observations = COALESCE(harness_observations, 0) + 1,
            invalid_action_count = COALESCE(invalid_action_count, 0) + 1
        WHERE run_id = ?
        """,
        (run_id,),
    )
    conn.commit()
    conn.close()


def finalize_logged_run(config: HarnessConfig, state: JsonDict) -> None:
    conn = _connect_log_db(config)
    run_id = _progress_current_run_id(config)
    if conn is None or run_id is None or state.get("state_type") != "game_over":
        if conn is not None:
            conn.close()
        return
    try:
        history = read_latest_history_run(config.run_setup.save_root)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        history = {}
    victory = _extract_run_victory(history)
    floor = _state_floor(state)
    if floor is None:
        floor = _extract_int(history, "floor", "floor_num", "floor_reached")
    conn.execute(
        """
        UPDATE runs
        SET end_time = ?, final_floor = ?, act = ?, boss_reached = ?,
            victory = ?, death_reason = ?
        WHERE run_id = ?
        """,
        (
            _now_utc(),
            floor,
            _state_act(state) or _extract_int(history, "act", "act_num"),
            1 if bool(history.get("boss_reached")) else 0,
            1 if victory else 0,
            history.get("death_reason") or state.get("death_reason"),
            run_id,
        ),
    )
    conn.commit()
    conn.close()


def _extract_int(value: JsonDict, *keys: str) -> int | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, int):
            return item
    return None


def auto_action_for_state(state: JsonDict, actions: list[Action]) -> Action | None:
    enabled_actions = [action for action in actions if action.enabled]
    if not enabled_actions or state.get("state_type") == "game_over":
        return None
    if state.get("state_type") == "rewards":
        for action in enabled_actions:
            if action.id.startswith("rewards_claim:") and "gold" in action.label:
                return action
    if len(enabled_actions) == 1:
        action = enabled_actions[0]
        if action.id in {
            "proceed_to_map",
            "event_advance_dialogue",
            "crystal_sphere_proceed",
            "combat_confirm_selection",
            "deck_confirm_selection",
            "bundle_confirm_selection",
        }:
            return action
        if action.category == "map":
            return action
    return None


def _state_summary(state: JsonDict) -> JsonDict:
    player = _player(state)
    summary: JsonDict = {
        "state_type": state.get("state_type"),
        "act": _state_act(state),
        "floor": _state_floor(state),
    }
    if player:
        summary["player"] = {
            "hp": player.get("hp"),
            "max_hp": player.get("max_hp"),
            "gold": player.get("gold"),
        }
    return summary


def resolve_auto_actions(
    client: Sts2Client,
    state: JsonDict,
    config: HarnessConfig,
    *,
    wait: float = 0.5,
) -> tuple[JsonDict, list[JsonDict]]:
    if not config.auto_resolve:
        return state, []
    auto_actions: list[JsonDict] = []
    for _ in range(max(0, config.max_auto_actions)):
        actions = build_actions(state, config.run_setup)
        action = auto_action_for_state(state, actions)
        if action is None:
            break
        result = client.post_action(action.request)
        log_step(config, state, actions, action, action_source="auto")
        if wait > 0:
            time.sleep(wait)
        state = _wait_for_play_phase(client)
        auto_actions.append(
            {
                "action": action.as_dict(actions.index(action)),
                "result": result,
                "state_after": _state_summary(state),
            }
        )
    return state, auto_actions


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def command_state(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    state = client.get_state(response_format=args.format)
    if args.format == "json":
        print_json(state)
    else:
        print(state)
    return 0


def command_actions(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    config = load_harness_config(args.config)
    state = _wait_for_play_phase(client)
    state, auto_actions = resolve_auto_actions(client, state, config)
    actions = action_dicts(build_actions(state, config.run_setup))
    output: JsonDict = {"state_type": state.get("state_type"), "actions": actions}
    if auto_actions:
        output["auto_actions"] = auto_actions
    print_json(output)
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    config = load_harness_config(args.config)
    state = _wait_for_play_phase(client)
    state, auto_actions = resolve_auto_actions(client, state, config)
    actions = action_dicts(build_actions(state, config.run_setup))
    output: JsonDict = {"state": state, "actions": actions}
    if auto_actions:
        output["auto_actions"] = auto_actions
    progress_update = maybe_update_progress_after_state(client, state, config.run_setup)
    if progress_update is not None:
        output["progress_update"] = progress_update
    finalize_logged_run(config, state)
    print_json(output)
    return 0


def command_act(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    config = load_harness_config(args.config)
    before = _wait_for_play_phase(client)
    before, pre_auto_actions = resolve_auto_actions(client, before, config)
    actions = build_actions(before, config.run_setup)
    try:
        action = find_action(actions, args.action)
    except ValueError as exc:
        error = str(exc)
        log_invalid_action(config, before, actions, args.action, error)
        output: JsonDict = {
            "status": "error",
            "error": error,
            "action_ref": args.action,
            "state": before,
            "actions": action_dicts(actions),
        }
        if pre_auto_actions:
            output["pre_auto_actions"] = pre_auto_actions
        progress_update = maybe_update_progress_after_state(
            client, before, config.run_setup
        )
        if progress_update is not None:
            output["progress_update"] = progress_update
        finalize_logged_run(config, before)
        print_json(output)
        return 1
    start_action = (
        action.request.get("action") == "menu_select"
        and str(action.request.get("option") or "").lower() in {"confirm", "embark"}
    )
    if not start_action:
        log_step(config, before, actions, action, action_source="agent")
    try:
        result = client.post_action(action.request)
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if not _is_timeout(exc):
            raise

        output: JsonDict = {
            "pre_auto_actions": pre_auto_actions,
            "action": action.as_dict(actions.index(action)),
            "result": {
                "status": "timeout_uncertain",
                "error": (
                    "Timed out waiting for STS2MCP to respond. The action may "
                    "or may not have been applied; compare the returned state "
                    "with the attempted action."
                ),
            },
        }
        if args.wait > 0:
            time.sleep(args.wait)
        try:
            after = _wait_for_play_phase(client)
            after, post_auto_actions = resolve_auto_actions(client, after, config)
            output["state"] = after
            output["actions"] = action_dicts(build_actions(after, config.run_setup))
            if post_auto_actions:
                output["auto_actions"] = post_auto_actions
            progress_update = maybe_update_progress_after_state(
                client, after, config.run_setup
            )
            if progress_update is not None:
                output["progress_update"] = progress_update
            finalize_logged_run(config, after)
            verification = verify_started_run_setup(action, config.run_setup)
            if verification is not None:
                output["run_setup_verification"] = verification
                log_start = start_logged_run(config, after, verification)
                if log_start is not None:
                    output["run_log"] = log_start
                    log_step(
                        config, before, actions, action, action_source="agent"
                    )
        except Exception as followup_exc:
            output["followup_error"] = str(followup_exc)
        print_json(output)
        return 0

    output: JsonDict = {
        "pre_auto_actions": pre_auto_actions,
        "action": action.as_dict(actions.index(action)),
        "result": result,
    }
    if not pre_auto_actions:
        output.pop("pre_auto_actions")

    if args.wait > 0:
        time.sleep(args.wait)
    after = _wait_for_play_phase(client)
    after, post_auto_actions = resolve_auto_actions(client, after, config)
    output["state"] = after
    output["actions"] = action_dicts(build_actions(after, config.run_setup))
    if post_auto_actions:
        output["auto_actions"] = post_auto_actions
    progress_update = maybe_update_progress_after_state(client, after, config.run_setup)
    if progress_update is not None:
        output["progress_update"] = progress_update
    finalize_logged_run(config, after)
    verification = verify_started_run_setup(action, config.run_setup)
    if verification is not None:
        output["run_setup_verification"] = verification
        log_start = start_logged_run(config, after, verification)
        if log_start is not None:
            output["run_log"] = log_start
            log_step(config, before, actions, action, action_source="agent")

    print_json(output)
    status = result.get("status")
    return 0 if status in {None, "ok"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sts2harness",
        description="State-scoped CLI harness for STS2MCP.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="Harness-managed run setup config JSON.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--mcp-delay",
        type=float,
        default=DEFAULT_MCP_DELAY,
        help=(
            "Minimum seconds between STS2MCP HTTP calls across harness processes. "
            "Use 0 only for diagnostics."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    state_parser = subparsers.add_parser("state", help="Print raw STS2MCP game state.")
    state_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    state_parser.set_defaults(func=command_state)

    actions_parser = subparsers.add_parser(
        "actions",
        help="Print currently legal high-level actions.",
    )
    actions_parser.set_defaults(func=command_actions)

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Print raw state plus currently legal high-level actions.",
    )
    snapshot_parser.set_defaults(func=command_snapshot)

    act_parser = subparsers.add_parser(
        "act",
        aliases=("submit",),
        help="Execute a currently legal action by index or ID.",
    )
    act_parser.add_argument("action", help="Action index or action ID from `actions`.")
    act_parser.add_argument(
        "--wait",
        type=float,
        default=2.0,
        help="Seconds to wait before reading the next state.",
    )
    act_parser.set_defaults(func=command_act)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print_json({"status": "error", "http_status": exc.code, "error": body})
        return 1
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if _is_timeout(exc):
            print_json({"status": "timeout", "error": "Timed out waiting for STS2MCP"})
        else:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            print_json(
                {"status": "error", "error": f"Could not reach STS2MCP: {reason}"}
            )
        return 1
    except (ValueError, KeyError, TypeError) as exc:
        print_json({"status": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
