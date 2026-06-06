from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_BASE_URL = "http://localhost:15526"
DEFAULT_MCP_DELAY = 1.0
DEFAULT_THROTTLE_FILE = "/tmp/sts2harness-mcp-throttle"
DEFAULT_CONFIG_FILE = "sts2harness.json"
DEFAULT_PROGRESS_FILE = ".sts2harness-progress.json"
DEFAULT_SAVE_ROOT = "~/.local/share/SlayTheSpire2/steam"


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class RunSetup:
    seed: str | None = None
    ascension: int | None = None
    character: str | None = None
    progress_file: str = DEFAULT_PROGRESS_FILE
    save_root: str = DEFAULT_SAVE_ROOT
    increment_ascension_on_win: bool = True


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    category: str
    request: JsonDict
    notes: tuple[str, ...] = ()

    def as_dict(self, index: int | None = None) -> JsonDict:
        result: JsonDict = {
            "id": self.id,
            "label": self.label,
            "category": self.category,
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


def load_run_setup(config_path: str) -> RunSetup:
    config = _load_json_file(config_path)
    run_config = config.get("run_setup")
    if not isinstance(run_config, dict):
        run_config = config

    seed = run_config.get("seed")
    character = run_config.get("character")
    progress_file = str(run_config.get("progress_file") or DEFAULT_PROGRESS_FILE)
    save_root = str(run_config.get("save_root") or DEFAULT_SAVE_ROOT)
    increment_on_win = run_config.get("increment_ascension_on_win")
    if increment_on_win is None:
        increment_on_win = True

    setup = RunSetup(
        seed=str(seed).strip() if seed is not None and str(seed).strip() else None,
        ascension=_optional_int(run_config.get("ascension")),
        character=str(character).strip().upper()
        if character is not None and str(character).strip()
        else None,
        progress_file=progress_file,
        save_root=save_root,
        increment_ascension_on_win=bool(increment_on_win),
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
            ascension=_optional_int(env_ascension)
            if env_ascension is not None
            else setup.ascension,
            character=env_character.strip().upper()
            if env_character is not None and env_character.strip()
            else setup.character,
            progress_file=setup.progress_file,
            save_root=env_save_root.strip()
            if env_save_root is not None and env_save_root.strip()
            else setup.save_root,
            increment_ascension_on_win=setup.increment_ascension_on_win,
        )

    progress = _load_json_file(setup.progress_file)
    current_ascension = progress.get("ascension")
    if isinstance(current_ascension, int):
        setup = RunSetup(
            seed=setup.seed,
            ascension=current_ascension,
            character=setup.character,
            progress_file=setup.progress_file,
            save_root=setup.save_root,
            increment_ascension_on_win=setup.increment_ascension_on_win,
        )
    elif setup.ascension is not None:
        progress["ascension"] = setup.ascension
        _write_json_file(setup.progress_file, progress)

    return setup


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
    player = _player(state)
    actions: list[Action] = []

    if battle.get("is_play_phase") is not True or battle.get("turn") != "player":
        return actions

    hand = player.get("hand")
    enemies = _alive_enemies(state)
    if isinstance(hand, list):
        for card in hand:
            if not isinstance(card, dict) or card.get("can_play") is not True:
                continue
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

    actions.append(
        Action(
            id="end_turn",
            label="End turn",
            category="combat",
            request={"action": "end_turn"},
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
        if reward_type == "potion" and potion_full:
            continue
        label = _reward_label(item, index)
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
        if 0 <= index < len(actions):
            return actions[index]
        raise ValueError(f"action index {index} is not currently legal")

    matches = [action for action in actions if action.id == action_ref]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"action_id {action_ref!r} is not currently legal")
    raise ValueError(
        f"action_id {action_ref!r} is ambiguous; use one of: "
        + ", ".join(action.id for action in matches)
    )


def _latest_history_entry(compendium: JsonDict) -> JsonDict | None:
    sections = compendium.get("sections")
    if not isinstance(sections, dict):
        return None
    run_history = sections.get("run_history")
    if not isinstance(run_history, dict):
        return None
    entries = run_history.get("entries")
    if not isinstance(entries, list) or not entries:
        return None

    dict_entries = [entry for entry in entries if isinstance(entry, dict)]
    if not dict_entries:
        return None

    def sort_key(entry: JsonDict) -> tuple[str, str]:
        return (
            str(entry.get("last_write_time_utc") or ""),
            str(entry.get("start_time") or entry.get("id") or ""),
        )

    return max(dict_entries, key=sort_key)


def maybe_update_progress_after_state(
    client: Sts2Client, state: JsonDict, run_setup: RunSetup
) -> JsonDict | None:
    if (
        not run_setup.increment_ascension_on_win
        or state.get("state_type") != "game_over"
    ):
        return None

    compendium = client.get_compendium()
    latest = _latest_history_entry(compendium)
    if latest is None:
        return None

    run_id = latest.get("run_id") or latest.get("id")
    if not run_id or latest.get("win") is not True:
        return None

    progress = _load_json_file(run_setup.progress_file)
    if progress.get("last_processed_win_run_id") == run_id:
        return None

    current_ascension = progress.get("ascension")
    if not isinstance(current_ascension, int):
        current_ascension = (
            run_setup.ascension if run_setup.ascension is not None else 0
        )

    progress["ascension"] = current_ascension + 1
    progress["last_processed_win_run_id"] = run_id
    progress["last_win_seed"] = latest.get("seed")
    progress["last_win_run_id"] = run_id
    _write_json_file(run_setup.progress_file, progress)

    return {
        "status": "updated",
        "reason": "win_detected",
        "run_id": run_id,
        "seed": latest.get("seed"),
        "previous_ascension": current_ascension,
        "next_ascension": progress["ascension"],
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
    while True:
        try:
            current_run = read_latest_current_run(run_setup.save_root)
            rng = current_run.get("rng")
            actual_seed = rng.get("seed") if isinstance(rng, dict) else None
            actual_ascension = current_run.get("ascension")

            expected_seed = request.get("seed")
            expected_ascension = request.get("ascension")
            seed_ok = expected_seed is None or str(actual_seed) == str(expected_seed)
            ascension_ok = (
                expected_ascension is None or actual_ascension == expected_ascension
            )

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
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                return {
                    "status": "error",
                    "error": last_error,
                    "save_root": os.path.expanduser(run_setup.save_root),
                }
            time.sleep(0.25)


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
    run_setup = load_run_setup(args.config)
    state = _wait_for_play_phase(client)
    actions = action_dicts(build_actions(state, run_setup))
    print_json({"state_type": state.get("state_type"), "actions": actions})
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    run_setup = load_run_setup(args.config)
    state = _wait_for_play_phase(client)
    actions = action_dicts(build_actions(state, run_setup))
    output: JsonDict = {"state": state, "actions": actions}
    progress_update = maybe_update_progress_after_state(client, state, run_setup)
    if progress_update is not None:
        output["progress_update"] = progress_update
    print_json(output)
    return 0


def command_act(args: argparse.Namespace) -> int:
    client = Sts2Client(args.base_url, timeout=args.timeout, mcp_delay=args.mcp_delay)
    run_setup = load_run_setup(args.config)
    before = _wait_for_play_phase(client)
    actions = build_actions(before, run_setup)
    action = find_action(actions, args.action)
    try:
        result = client.post_action(action.request)
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        if not _is_timeout(exc):
            raise

        output: JsonDict = {
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
        if not args.no_after:
            if args.wait > 0:
                time.sleep(args.wait)
            try:
                after = _wait_for_play_phase(client)
                output["state"] = after
                output["actions"] = action_dicts(build_actions(after, run_setup))
                progress_update = maybe_update_progress_after_state(
                    client, after, run_setup
                )
                if progress_update is not None:
                    output["progress_update"] = progress_update
                verification = verify_started_run_setup(action, run_setup)
                if verification is not None:
                    output["run_setup_verification"] = verification
            except Exception as followup_exc:
                output["followup_error"] = str(followup_exc)
        print_json(output)
        return 0

    output: JsonDict = {
        "action": action.as_dict(actions.index(action)),
        "result": result,
    }

    if not args.no_after:
        if args.wait > 0:
            time.sleep(args.wait)
        after = _wait_for_play_phase(client)
        output["state"] = after
        output["actions"] = action_dicts(build_actions(after, run_setup))
        progress_update = maybe_update_progress_after_state(client, after, run_setup)
        if progress_update is not None:
            output["progress_update"] = progress_update
        verification = verify_started_run_setup(action, run_setup)
        if verification is not None:
            output["run_setup_verification"] = verification

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
        "--no-after",
        action="store_true",
        help="Only print the action result, not the next state/actions.",
    )
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
