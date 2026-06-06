import unittest
import json
import os
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
            {
                "action": "menu_select",
                "option": "confirm",
                "seed": "ABC123",
                "ascension": 4,
            },
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

        self.assertEqual(
            [action.id for action in actions], ["menu:silent", "menu:back"]
        )

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

        self.assertEqual(
            [action.id for action in actions], ["menu:custom", "menu:back"]
        )


class ProgressTests(unittest.TestCase):
    def test_history_win_increments_ascension_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = str(Path(tmpdir) / "progress.json")
            history = (
                Path(tmpdir)
                / "steamid"
                / "modded"
                / "profile1"
                / "saves"
                / "history"
            )
            history.mkdir(parents=True)
            history_file = history / "run1.run"
            history_file.write_text(
                json.dumps(
                    {
                        "run_id": "modded:profile1:10",
                        "seed": "ABC123",
                        "ascension": 1,
                        "victory": True,
                    }
                ),
                encoding="utf-8",
            )
            setup = main.RunSetup(
                seed="ABC123",
                ascension=1,
                progress_file=progress_file,
                save_root=tmpdir,
            )

            first = main.maybe_update_progress_after_state(
                FakeClient([]), {"state_type": "game_over"}, setup
            )
            second = main.maybe_update_progress_after_state(
                FakeClient([]), {"state_type": "game_over"}, setup
            )

            self.assertEqual(first["next_ascension"], 2)
            self.assertIsNone(second)
            progress = main._load_json_file(progress_file)
            self.assertEqual(progress["ascension"], 2)

    def test_a10_win_advances_seed_and_stops_after_three(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = str(Path(tmpdir) / "progress.json")
            history = Path(tmpdir) / "saves" / "history"
            history.mkdir(parents=True)
            setup = main.RunSetup(
                seed_set=("S1", "S2", "S3"),
                ascension=10,
                progress_file=progress_file,
                save_root=tmpdir,
                seed_policy="fixed_list_until_win",
                max_ascension=10,
                stop_after_consecutive_a10_wins=3,
            )
            for index, seed in enumerate(("S1", "S2", "S3"), start=1):
                history_file = history / f"run{index}.run"
                history_file.write_text(
                    json.dumps(
                        {
                            "run_id": f"run-{index}",
                            "seed": seed,
                            "ascension": 10,
                            "victory": True,
                        }
                    ),
                    encoding="utf-8",
                )
                os.utime(history_file, (1000 + index, 1000 + index))
                update = main.maybe_update_progress_after_state(
                    FakeClient([]), {"state_type": "game_over"}, setup
                )
                self.assertIsNotNone(update)

            progress = main._load_json_file(progress_file)
            self.assertEqual(progress["consecutive_a10_wins"], 3)
            self.assertTrue(progress["stopped"])


class CurrentRunVerificationTests(unittest.TestCase):
    def test_verify_started_run_setup_reads_newest_current_run_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            stale = Path(tmpdir) / "steamid" / "profile1" / "saves"
            fresh = Path(tmpdir) / "steamid" / "modded" / "profile2" / "saves"
            stale.mkdir(parents=True)
            fresh.mkdir(parents=True)
            (stale / "current_run.save").write_text(
                json.dumps({"rng": {"seed": "OLD"}, "ascension": 0, "start_time": 1}),
                encoding="utf-8",
            )
            fresh_file = fresh / "current_run.save"
            fresh_file.write_text(
                json.dumps(
                    {
                        "rng": {"seed": "ABC123"},
                        "ascension": 4,
                        "game_mode": "custom",
                        "start_time": 2,
                    }
                ),
                encoding="utf-8",
            )
            os.utime(stale / "current_run.save", (1000, 1000))
            os.utime(fresh_file, (2000, 2000))

            action = main.Action(
                id="menu:confirm",
                label="Select menu option: confirm",
                category="menu",
                request={
                    "action": "menu_select",
                    "option": "confirm",
                    "seed": "ABC123",
                    "ascension": 4,
                },
            )
            setup = main.RunSetup(save_root=tmpdir)

            verification = main.verify_started_run_setup(action, setup, timeout=0)

            self.assertEqual(verification["status"], "ok")
            self.assertEqual(verification["actual"]["seed"], "ABC123")
            self.assertEqual(verification["actual"]["ascension"], 4)
            self.assertEqual(verification["actual"]["game_mode"], "custom")


if __name__ == "__main__":
    unittest.main()
