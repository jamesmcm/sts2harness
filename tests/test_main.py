import unittest
import json
import os
import sqlite3
import subprocess
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

    def test_stop_after_current_run_marks_progress_stopped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = str(Path(tmpdir) / "progress.json")
            history = Path(tmpdir) / "saves" / "history"
            history.mkdir(parents=True)
            (history / "run1.run").write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "seed": "S1",
                        "ascension": 0,
                        "victory": False,
                    }
                ),
                encoding="utf-8",
            )
            setup = main.RunSetup(
                seed_set=("S1", "S2"),
                seed_policy="fixed_list",
                progress_file=progress_file,
                save_root=tmpdir,
                stop_after_current_run=True,
            )

            update = main.maybe_update_progress_after_state(
                FakeClient([]), {"state_type": "game_over"}, setup
            )

            self.assertTrue(update["stopped"])
            progress = main._load_json_file(progress_file)
            self.assertTrue(progress["stopped"])
            self.assertEqual(progress["stop_reason"], "stop_after_current_run")


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


class MemoryCommitTests(unittest.TestCase):
    def test_commit_memory_snapshot_commits_only_configured_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subprocess.run(["git", "init"], cwd=tmpdir, check=True, stdout=subprocess.PIPE)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=tmpdir,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=tmpdir,
                check=True,
            )
            Path(tmpdir, "STRATEGY.md").write_text("initial\n", encoding="utf-8")
            Path(tmpdir, "CURRENT_RUN.md").write_text("run\n", encoding="utf-8")
            Path(tmpdir, "BATTLE_LOG.md").write_text("log\n", encoding="utf-8")
            Path(tmpdir, "runs.sqlite").write_text("db\n", encoding="utf-8")
            config = main.HarnessConfig(
                run_setup=main.RunSetup(),
                logging=main.LoggingConfig(
                    memory_git_dir=tmpdir,
                    memory_commit_on_run_start=True,
                    memory_commit_paths=(
                        "STRATEGY.md",
                        "CURRENT_RUN.md",
                        "BATTLE_LOG.md",
                    ),
                ),
            )

            result = main.commit_memory_snapshot(config, "run-1")

            self.assertEqual(result["status"], "committed")
            tracked = subprocess.run(
                ["git", "ls-tree", "--name-only", "HEAD"],
                cwd=tmpdir,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            self.assertEqual(
                tracked, ["BATTLE_LOG.md", "CURRENT_RUN.md", "STRATEGY.md"]
            )


class InvalidActionLoggingTests(unittest.TestCase):
    def test_nested_run_floor_and_act_are_logged(self):
        state = {"state_type": "monster", "run": {"act": 3, "floor": 37}}

        self.assertEqual(main._state_floor(state), 37)
        self.assertEqual(main._state_act(state), 3)

    def test_log_invalid_action_records_step_and_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = str(Path(tmpdir) / "runs.sqlite")
            progress_file = str(Path(tmpdir) / "progress.json")
            config = main.HarnessConfig(
                run_setup=main.RunSetup(
                    seed="S1",
                    ascension=0,
                    progress_file=progress_file,
                ),
                agent=main.AgentConfig(
                    agent_name="agent",
                    model_name="model",
                    condition_name="condition",
                ),
                logging=main.LoggingConfig(
                    sqlite_path=sqlite_path,
                    official_run_logging=True,
                ),
            )
            state = {
                "state_type": "monster",
                "floor": 3,
                "player": {"hp": 10, "max_hp": 80, "gold": 5},
            }
            start = main.start_logged_run(config, state, None)
            self.assertEqual(start["status"], "started")

            main.log_invalid_action(config, state, [], "play_card:7", "not legal")

            conn = sqlite3.connect(sqlite_path)
            run_row = conn.execute(
                "SELECT invalid_action_count FROM runs WHERE run_id = ?",
                (start["run_id"],),
            ).fetchone()
            step_row = conn.execute(
                "SELECT action_source, action_chosen FROM steps WHERE run_id = ?",
                (start["run_id"],),
            ).fetchone()
            conn.close()
            self.assertEqual(run_row[0], 1)
            self.assertEqual(step_row[0], "invalid_agent")
            self.assertIn("play_card:7", step_row[1])


if __name__ == "__main__":
    unittest.main()
