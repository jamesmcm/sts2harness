import unittest
import tempfile
from pathlib import Path

import main


class FakeClient:
    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def get_state(self, *, response_format="json"):
        self.calls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]


class WaitForPlayPhaseTests(unittest.TestCase):
    def test_non_combat_returns_immediately(self):
        client = FakeClient([{"state_type": "map"}])

        state = main._wait_for_play_phase(client, poll_interval=0)

        self.assertEqual(state["state_type"], "map")
        self.assertEqual(client.calls, 1)

    def test_combat_waits_until_player_play_phase(self):
        client = FakeClient(
            [
                {
                    "state_type": "monster",
                    "battle": {"is_play_phase": False, "turn": "player"},
                },
                {
                    "state_type": "monster",
                    "battle": {"is_play_phase": True, "turn": "enemy"},
                },
                {
                    "state_type": "monster",
                    "battle": {"is_play_phase": True, "turn": "player"},
                },
            ]
        )

        state = main._wait_for_play_phase(client, poll_interval=0)

        self.assertEqual(state["battle"]["turn"], "player")
        self.assertIs(state["battle"]["is_play_phase"], True)
        self.assertEqual(client.calls, 3)


class RunSetupActionTests(unittest.TestCase):
    def test_custom_confirm_uses_harness_seed_and_ascension(self):
        state = {
            "state_type": "menu",
            "menu_screen": "custom_run",
            "selected_character": {"id": "IRONCLAD"},
            "options": [
                {"name": "confirm", "enabled": True},
                {"name": "IRONCLAD", "enabled": True},
            ],
        }
        setup = main.RunSetup(seed="ABC123", ascension=4)

        actions = main.build_actions(state, setup)

        self.assertEqual(actions[0].id, "menu:confirm")
        self.assertEqual(
            actions[0].request,
            {"action": "menu_select", "option": "confirm", "seed": "ABC123", "ascension": 4},
        )

    def test_configured_character_filters_custom_run_character_choices(self):
        state = {
            "state_type": "menu",
            "menu_screen": "custom_run",
            "options": [
                {"name": "IRONCLAD", "enabled": True},
                {"name": "SILENT", "enabled": True},
                {"name": "back", "enabled": True},
            ],
        }
        setup = main.RunSetup(character="SILENT")

        actions = main.build_actions(state, setup)

        self.assertEqual([action.id for action in actions], ["menu:silent", "menu:back"])

    def test_seeded_setup_filters_singleplayer_to_custom(self):
        state = {
            "state_type": "menu",
            "menu_screen": "singleplayer",
            "options": [
                {"name": "standard", "enabled": True},
                {"name": "daily", "enabled": True},
                {"name": "custom", "enabled": True},
                {"name": "back", "enabled": True},
            ],
        }
        setup = main.RunSetup(seed="ABC123")

        actions = main.build_actions(state, setup)

        self.assertEqual([action.id for action in actions], ["menu:custom", "menu:back"])


class ProgressTests(unittest.TestCase):
    def test_win_increments_ascension_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = str(Path(tmpdir) / "progress.json")
            setup = main.RunSetup(seed="ABC123", ascension=1, progress_file=progress_file)
            client = FakeCompendiumClient(
                {
                    "sections": {
                        "run_history": {
                            "entries": [
                                {
                                    "run_id": "modded:profile1:10",
                                    "last_write_time_utc": "2026-06-06T10:00:00Z",
                                    "seed": "ABC123",
                                    "win": True,
                                }
                            ]
                        }
                    }
                }
            )

            first = main.maybe_update_progress_after_state(client, {"state_type": "game_over"}, setup)
            second = main.maybe_update_progress_after_state(client, {"state_type": "game_over"}, setup)

            self.assertEqual(first["next_ascension"], 2)
            self.assertIsNone(second)
            progress = main._load_json_file(progress_file)
            self.assertEqual(progress["ascension"], 2)


class FakeCompendiumClient:
    def __init__(self, compendium):
        self.compendium = compendium

    def get_compendium(self):
        return self.compendium


if __name__ == "__main__":
    unittest.main()
