import io
import http.client
import unittest
import json
import os
import sqlite3
import subprocess
import tempfile
import urllib.error
from unittest import mock
from pathlib import Path

import main
from pi_agent import rpc_server
from pi_orchestrator import orchestrator


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

    def test_post_end_turn_waits_past_short_transitional_hand(self):
        before = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player"},
            "player": {
                "energy": 0,
                "max_energy": 3,
                "hand": [{"index": 0, "name": "Strike"}],
                "draw_pile_count": 8,
                "discard_pile_count": 1,
                "relics": [],
            },
        }
        partial = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 3,
                "max_energy": 3,
                "hand": [{"index": 0, "name": "Defend", "can_play": True}],
            },
        }
        ready = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 3,
                "max_energy": 3,
                "hand": [
                    {"index": index, "name": f"card {index}", "can_play": True}
                    for index in range(5)
                ],
            },
        }
        action = main.Action(
            id="end_turn",
            label="End turn",
            category="combat",
            request={"action": "end_turn"},
        )
        client = FakeClient([partial, ready])

        state = main._wait_for_post_action_state(
            client,
            previous_state=before,
            previous_action=action,
            poll_interval=0,
        )

        self.assertIs(state, ready)
        self.assertEqual(client.calls, 2)

    def test_post_end_turn_waits_for_paels_tears_energy(self):
        before = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player"},
            "player": {
                "energy": 1,
                "max_energy": 3,
                "hand": [{"index": 0, "name": "Strike"}],
                "draw_pile_count": 8,
                "discard_pile_count": 1,
                "relics": [{"name": "Pael's Tears"}],
            },
        }
        partial = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 3,
                "max_energy": 3,
                "hand": [
                    {"index": index, "name": f"card {index}", "can_play": True}
                    for index in range(5)
                ],
            },
        }
        ready = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 5,
                "max_energy": 3,
                "hand": [
                    {"index": index, "name": f"card {index}", "can_play": True}
                    for index in range(5)
                ],
            },
        }
        action = main.Action(
            id="end_turn",
            label="End turn",
            category="combat",
            request={"action": "end_turn"},
        )
        client = FakeClient([partial, ready])

        state = main._wait_for_post_action_state(
            client,
            previous_state=before,
            previous_action=action,
            poll_interval=0,
        )

        self.assertIs(state, ready)
        self.assertEqual(client.calls, 2)


class PiRpcPostActionWaitTests(unittest.TestCase):
    def test_act_uses_post_action_readiness_wait(self):
        server = rpc_server.PiRpcServer.__new__(rpc_server.PiRpcServer)
        server.config = rpc_server.PiAgentConfig(
            harness_config="unused.json",
            memory_root="unused",
            wait_after_action=0,
        )
        server.harness_config = mock.Mock(
            run_setup=None,
            auto_resolve=False,
        )
        server.client = mock.Mock()
        before = {"state_type": "monster"}
        after = {"state_type": "monster"}
        action = main.Action(
            id="end_turn",
            label="End turn",
            category="combat",
            request={"action": "end_turn"},
        )
        server.client.post_action.return_value = {"status": "ok"}

        with (
            mock.patch.object(server, "_reload_harness_config"),
            mock.patch.object(rpc_server.harness, "_wait_for_play_phase", return_value=before),
            mock.patch.object(
                rpc_server.harness,
                "_wait_for_post_action_state",
                return_value=after,
            ) as wait_for_post_action,
            mock.patch.object(
                rpc_server.harness,
                "resolve_auto_actions",
                side_effect=[(before, []), (after, [])],
            ),
            mock.patch.object(rpc_server.harness, "build_actions", return_value=[action]),
            mock.patch.object(rpc_server.harness, "find_action", return_value=action),
            mock.patch.object(rpc_server.harness, "log_step"),
            mock.patch.object(
                rpc_server.harness,
                "refresh_harness_config_from_progress",
                return_value=server.harness_config,
            ),
            mock.patch.object(rpc_server.harness, "action_dicts", return_value=[]),
            mock.patch.object(
                rpc_server.harness,
                "maybe_update_progress_after_state",
                return_value=None,
            ),
            mock.patch.object(rpc_server.harness, "finalize_logged_run"),
            mock.patch.object(
                rpc_server.harness,
                "maybe_commit_memory_checkpoint",
                return_value=None,
            ),
            mock.patch.object(
                rpc_server.harness,
                "verify_started_run_setup",
                return_value=None,
            ),
        ):
            server.act({"action": "end_turn", "wait": 0})

        wait_for_post_action.assert_called_once_with(
            server.client,
            previous_state=before,
            previous_action=action,
        )


class PiOrchestratorConfigTests(unittest.TestCase):
    def test_opencode_go_provider_defaults_to_deepseek_flash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orchestrator.json"
            path.write_text(
                json.dumps(
                    {
                        "rpc_server_command": ["python", "pi_agent/rpc_server.py"],
                        "model": {"provider": "opencode_go"},
                    }
                ),
                encoding="utf-8",
            )

            config = orchestrator.load_config(path)

        self.assertEqual(config.model.provider, "opencode_go")
        self.assertEqual(config.model.model, "deepseek-v4-flash")
        self.assertIsNone(config.max_steps)

    def test_max_steps_is_optional_positive_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orchestrator.json"
            path.write_text(
                json.dumps(
                    {
                        "rpc_server_command": ["python", "pi_agent/rpc_server.py"],
                        "model": {"provider": "opencode_go"},
                        "max_steps": 12,
                    }
                ),
                encoding="utf-8",
            )

            config = orchestrator.load_config(path)

        self.assertEqual(config.max_steps, 12)

    def test_agent_context_config_defaults_off_and_loads_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "orchestrator.json"
            path.write_text(
                json.dumps(
                    {
                        "rpc_server_command": ["python", "pi_agent/rpc_server.py"],
                        "model": {"provider": "opencode_go"},
                        "agent_context": {
                            "enabled": True,
                            "compaction_token_threshold": 1234,
                            "prompt_cache": True,
                        },
                    }
                ),
                encoding="utf-8",
            )

            config = orchestrator.load_config(path)

        self.assertTrue(config.agent_context.enabled)
        self.assertEqual(config.agent_context.compaction_token_threshold, 1234)
        self.assertTrue(config.agent_context.prompt_cache)

    def test_opencode_go_provider_uses_go_endpoint_and_key_env_defaults(self):
        old_key = os.environ.get("OPENCODE_API_KEY")
        os.environ["OPENCODE_API_KEY"] = "test-key"
        try:
            provider = orchestrator.make_provider(
                orchestrator.ModelConfig(
                    provider="opencode_go",
                    model="deepseek-v4-flash",
                )
            )
        finally:
            if old_key is None:
                os.environ.pop("OPENCODE_API_KEY", None)
            else:
                os.environ["OPENCODE_API_KEY"] = old_key

        self.assertIsInstance(provider, orchestrator.OpenCodeGoProvider)
        self.assertEqual(provider.config.base_url, "https://opencode.ai/zen/go/v1")
        self.assertEqual(provider.config.api_key_env, "OPENCODE_API_KEY")
        self.assertEqual(provider.config.response_format, "json_object")

    def test_opencode_go_provider_reads_bridge_auth_file(self):
        old_home = os.environ.get("HOME")
        old_key = os.environ.pop("OPENCODE_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.environ["HOME"] = tmp
                auth_path = (
                    Path(tmp) / ".local" / "share" / "opencode" / "auth.json"
                )
                auth_path.parent.mkdir(parents=True)
                auth_path.write_text(
                    json.dumps({"opencode-go": {"type": "api", "key": "file-key"}}),
                    encoding="utf-8",
                )

                provider = orchestrator.make_provider(
                    orchestrator.ModelConfig(
                        provider="opencode_go",
                        model="deepseek-v4-flash",
                    )
                )
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            if old_key is not None:
                os.environ["OPENCODE_API_KEY"] = old_key

        self.assertEqual(provider.api_key, "file-key")


class FakeChatProvider(orchestrator.OpenAICompatibleChatProvider):
    def __init__(self, config, responses):
        self.config = config
        self.schema = {"type": "object"}
        self.api_key = "test-key"
        self.responses = list(responses)
        self.payloads = []

    def _post_json(self, url, payload):
        del url
        self.payloads.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PiOrchestratorResponseFormatTests(unittest.TestCase):
    def test_openai_compatible_defaults_to_json_schema(self):
        provider = FakeChatProvider(
            orchestrator.ModelConfig(
                provider="openai_compatible_chat",
                model="model",
                base_url="https://example.test/v1",
            ),
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"action_ref":"0","rationale":"ok"}'
                            }
                        }
                    ]
                }
            ],
        )

        provider.complete_json("prompt")

        self.assertEqual(
            provider.payloads[0]["response_format"]["type"], "json_schema"
        )

    def test_json_schema_unavailable_falls_back_to_json_object(self):
        provider = FakeChatProvider(
            orchestrator.ModelConfig(
                provider="openai_compatible_chat",
                model="model",
                base_url="https://example.test/v1",
            ),
            [
                RuntimeError(
                    "model API HTTP 400: response_format type is unavailable now"
                ),
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"action_ref":"0","rationale":"ok"}'
                            }
                        }
                    ]
                },
            ],
        )

        completion = provider.complete_json("prompt")

        self.assertEqual(completion.decision["action_ref"], "0")
        self.assertEqual(provider.payloads[0]["response_format"]["type"], "json_schema")
        self.assertEqual(provider.payloads[1]["response_format"]["type"], "json_object")

    def test_configured_none_omits_response_format(self):
        provider = FakeChatProvider(
            orchestrator.ModelConfig(
                provider="openai_compatible_chat",
                model="model",
                base_url="https://example.test/v1",
                response_format="none",
            ),
            [
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"action_ref":"0","rationale":"ok"}'
                            }
                        }
                    ]
                }
            ],
        )

        provider.complete_json("prompt")

        self.assertNotIn("response_format", provider.payloads[0])

    def test_cached_prompt_tokens_are_parsed_from_usage_details(self):
        completion = orchestrator._completion_from_text(
            {
                "usage": {
                    "prompt_tokens": 2000,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 1536},
                }
            },
            '{"action_ref":"0","rationale":"ok"}',
            {"model": "x"},
        )

        self.assertEqual(completion.input_tokens, 2000)
        self.assertEqual(completion.cached_input_tokens, 1536)


class FakeHttpResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return self.body


class RetryHttpProvider(orchestrator.HttpJsonProvider):
    def __init__(self, retry_backoffs):
        self.config = orchestrator.ModelConfig(
            provider="openai_compatible_chat",
            model="model",
            retry_backoffs=tuple(retry_backoffs),
        )
        self.schema = {"type": "object"}
        self.api_key = "test-key"


class PiOrchestratorHttpRetryTests(unittest.TestCase):
    def test_post_json_retries_transient_http_error(self):
        provider = RetryHttpProvider((1.5,))
        calls = []

        def fake_urlopen(request, timeout):
            del timeout
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "Service Unavailable",
                    {},
                    io.BytesIO(b"busy"),
                )
            return FakeHttpResponse(b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("time.sleep") as sleep:
                response = provider._post_json("https://example.test/v1", {"x": 1})

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(1.5)

    def test_post_json_preserves_final_http_error_body(self):
        provider = RetryHttpProvider((0,))

        def fake_urlopen(request, timeout):
            del timeout
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "Service Unavailable",
                {},
                io.BytesIO(b"still down"),
            )

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaisesRegex(RuntimeError, "model API HTTP 503: still down"):
                provider._post_json("https://example.test/v1", {"x": 1})

    def test_post_json_retries_remote_disconnected(self):
        provider = RetryHttpProvider((0,))
        calls = 0

        def fake_urlopen(request, timeout):
            nonlocal calls
            del request, timeout
            calls += 1
            if calls == 1:
                raise http.client.RemoteDisconnected(
                    "Remote end closed connection without response"
                )
            return FakeHttpResponse(b'{"ok": true}')

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with mock.patch("time.sleep") as sleep:
                response = provider._post_json("https://example.test/v1", {"x": 1})

        self.assertEqual(response, {"ok": True})
        self.assertEqual(calls, 2)
        sleep.assert_not_called()


class MalformedJsonProvider(orchestrator.ModelProvider):
    def __init__(self, failures_before_success=None):
        self.calls = 0
        self.failures_before_success = failures_before_success

    def complete_json(self, prompt):
        del prompt
        self.calls += 1
        if (
            self.failures_before_success is not None
            and self.calls > self.failures_before_success
        ):
            return orchestrator.ModelCompletion(
                decision={"action_ref": "0", "rationale": "ok"},
                raw_response={"ok": True},
                response_text='{"action_ref":"0","rationale":"ok"}',
                request_payload={"model": "x"},
            )
        raise orchestrator.ModelResponseFormatError(
            "model returned invalid decision JSON: bad",
            raw_response={"bad": True, "call": self.calls},
            response_text='{"action_ref":"0", "memory_updates": [}',
            request_payload={"model": "x"},
        )


class PiOrchestratorModelResponseRetryTests(unittest.TestCase):
    def _config(self, log_path):
        return orchestrator.OrchestratorConfig(
            rpc_server_command=["python", "pi_agent/rpc_server.py"],
            model=orchestrator.ModelConfig(provider="openai_responses", model="x"),
            decision_log=str(log_path),
        )

    def test_invalid_model_json_is_retried_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            provider = MalformedJsonProvider(failures_before_success=1)

            with mock.patch("time.sleep") as sleep:
                completion = orchestrator.complete_with_response_retries(
                    provider,
                    self._config(log_path),
                    "prompt",
                    7,
                )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(provider.calls, 2)
        sleep.assert_called_once_with(orchestrator.MODEL_DECISION_PARSE_RETRY_DELAY)
        self.assertEqual(completion.decision["action_ref"], "0")
        self.assertEqual(records[0]["event"], "invalid_model_json")
        self.assertFalse(records[0]["final_attempt"])

    def test_invalid_model_json_final_attempt_returns_rejected_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            provider = MalformedJsonProvider()

            with mock.patch("time.sleep"):
                completion = orchestrator.complete_with_response_retries(
                    provider,
                    self._config(log_path),
                    "prompt",
                    8,
                )

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(provider.calls, orchestrator.MODEL_DECISION_PARSE_RETRIES + 1)
        self.assertEqual(completion.decision["action_ref"], "")
        self.assertIn('"memory_updates"', completion.response_text)
        self.assertTrue(records[-1]["final_attempt"])


class FakeRpc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method != "snapshot":
            raise AssertionError(f"unexpected method: {method}")
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class PiOrchestratorSnapshotRetryTests(unittest.TestCase):
    def _config(self, log_path, **overrides):
        values = {
            "rpc_server_command": ["python", "pi_agent/rpc_server.py"],
            "model": orchestrator.ModelConfig(provider="openai_responses", model="x"),
            "decision_log": str(log_path),
            "empty_action_poll_interval": 0,
        }
        values.update(overrides)
        return orchestrator.OrchestratorConfig(**values)

    def test_snapshot_with_actions_retries_empty_action_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeRpc(
                [
                    {"state": {"state_type": "event"}, "actions": []},
                    {
                        "state": {"state_type": "event"},
                        "actions": [{"id": "event:0"}],
                    },
                ]
            )

            snapshot, state, actions = orchestrator.snapshot_with_actions(
                rpc, self._config(Path(tmp) / "decisions.jsonl"), 3
            )

        self.assertEqual(snapshot["state"]["state_type"], "event")
        self.assertEqual(state["state_type"], "event")
        self.assertEqual(actions, [{"id": "event:0"}])
        self.assertEqual(len(rpc.calls), 2)

    def test_snapshot_with_actions_times_out_after_empty_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpc = FakeRpc([{"state": {"state_type": "event"}, "actions": []}])

            with self.assertRaisesRegex(RuntimeError, "no legal actions"):
                orchestrator.snapshot_with_actions(
                    rpc,
                    self._config(
                        Path(tmp) / "decisions.jsonl",
                        empty_action_max_wait=0,
                    ),
                    3,
                )

    def test_memory_updates_are_full_writes_to_known_note_files(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))
                if method == "read_memory":
                    return {"content": "# Existing\n\n" + ("Useful prior context.\n" * 5)}
                return {"status": "ok"}

        rpc = FakeRpc()
        current_run_content = (
            "# Current Run\n\n"
            + ("Deck, relic, potion, path, risk, and tactic detail.\n" * 35)
        )
        written = orchestrator.apply_memory_updates(
            rpc,
            {
                "memory_updates": [
                    {
                        "path": "CURRENT_RUN.md",
                        "mode": "write",
                        "content": current_run_content,
                    },
                    {
                        "path": "HARNESS_BUGS.md",
                        "mode": "write",
                        "content": "# Harness Bugs\n\nHarness issue.\n",
                    },
                    {
                        "path": "STRATEGY.md",
                        "mode": "append",
                        "content": "\nIgnored append.\n",
                    },
                    {
                        "path": "other.md",
                        "mode": "write",
                        "content": "Ignored.",
                    },
                ]
            },
        )

        self.assertEqual(written, {"CURRENT_RUN.md", "HARNESS_BUGS.md"})
        self.assertEqual(
            rpc.calls,
            [
                (
                    "read_memory",
                    {
                        "path": "CURRENT_RUN.md",
                    },
                ),
                (
                    "write_memory",
                    {
                        "path": "CURRENT_RUN.md",
                        "content": current_run_content,
                    },
                ),
                (
                    "read_memory",
                    {
                        "path": "HARNESS_BUGS.md",
                    },
                ),
                (
                    "write_memory",
                    {
                        "path": "HARNESS_BUGS.md",
                        "content": "# Harness Bugs\n\nHarness issue.\n",
                    },
                )
            ],
        )

    def test_memory_write_requires_successful_preread(self):
        class FakeRpc:
            def call(self, method, params=None):
                del params
                if method == "read_memory":
                    return {"error": "missing content"}
                raise AssertionError("write should not happen without preread content")

        with self.assertRaisesRegex(RuntimeError, "could not read current"):
            orchestrator.apply_memory_updates(
                FakeRpc(),
                {
                    "memory_updates": [
                        {
                            "path": "HARNESS_BUGS.md",
                            "mode": "write",
                            "content": "# Harness Bugs\n\nBug note.\n",
                        }
                    ]
                },
            )

    def test_finished_battle_requires_current_run_rewrite_before_advancing(self):
        snapshot = {
            "state": {
                "state_type": "rewards",
                "battle": {"enemies": [{"name": "Jaw Worm", "hp": 0}]},
            },
            "actions": [{"id": "0"}],
        }
        memory = {
            "BATTLE_LOG.md": "# Battle Log\n\nKilled Jaw Worm. Took 8. Bash was key.\n"
        }

        error = orchestrator.validate_memory_lifecycle(
            {"action_ref": "0"}, snapshot, memory
        )

        self.assertIn("rewrite CURRENT_RUN.md", error)

    def test_rewards_without_dead_enemy_evidence_does_not_require_current_run_rewrite(self):
        snapshot = {"state": {"state_type": "rewards"}, "actions": [{"id": "0"}]}
        memory = {
            "BATTLE_LOG.md": "# Battle Log\n\nPossibly stale combat note.\n"
        }

        error = orchestrator.validate_memory_lifecycle(
            {"action_ref": "0"}, snapshot, memory
        )

        self.assertIsNone(error)

    def test_combat_hand_select_does_not_require_current_run_rewrite(self):
        snapshot = {
            "state": {
                "state_type": "hand_select",
                "battle": {"is_play_phase": True, "turn": "player"},
                "hand_select": {"prompt": "Choose a card to Exhaust."},
            },
            "actions": [{"id": "combat_select_card:0"}],
        }
        memory = {
            "BATTLE_LOG.md": "# Battle Log\n\nPlayed True Grit+; exhaust Defend.\n"
        }

        error = orchestrator.validate_memory_lifecycle(
            {"action_ref": "0"}, snapshot, memory
        )

        self.assertIsNone(error)

    def test_finished_battle_lifecycle_prompt_warns_battle_log_will_be_cleared(self):
        snapshot = {
            "state": {
                "state_type": "rewards",
                "battle": {"enemies": [{"name": "Jaw Worm", "dead": True}]},
            },
            "actions": [{"id": "0"}],
        }
        memory = {
            "BATTLE_LOG.md": "# Battle Log\n\nKilled Jaw Worm. Took 8. Bash was key.\n"
        }

        requirement = orchestrator.build_lifecycle_requirement(snapshot, memory)

        self.assertIn("complete battle outcome", requirement)
        self.assertIn("only persistent context for this run", requirement)
        self.assertIn("do not make a sparse summary", requirement)
        self.assertIn("same prompt's `Memory` section", requirement)
        self.assertIn("preserve every useful lesson from `BATTLE_LOG.md`", requirement)
        self.assertIn("clear `BATTLE_LOG.md`", requirement)
        self.assertIn("must stand on its own", requirement)

    def test_finished_battle_accepts_current_run_rewrite(self):
        snapshot = {
            "state": {
                "state_type": "rewards",
                "battle": {"enemies": [{"name": "Jaw Worm", "is_dead": True}]},
            },
            "actions": [{"id": "0"}],
        }
        memory = {
            "BATTLE_LOG.md": "# Battle Log\n\nKilled Jaw Worm. Took 8. Bash was key.\n"
        }

        error = orchestrator.validate_memory_lifecycle(
            {
                "action_ref": "0",
                "memory_updates": [
                    {
                        "path": "CURRENT_RUN.md",
                        "mode": "write",
                        "content": "# Current Run\n\nFolded combat details.\n",
                    }
                ],
            },
            snapshot,
            memory,
        )

        self.assertIsNone(error)

    def test_game_over_requires_strategy_rewrite_before_advancing(self):
        snapshot = {
            "state": {"state_type": "game_over"},
            "actions": [{"id": "menu:main_menu"}],
            "progress_update": {"last_completed_victory": False},
        }

        error = orchestrator.validate_memory_lifecycle(
            {
                "action_ref": "menu:main_menu",
                "memory_updates": [
                    {
                        "path": "CURRENT_RUN.md",
                        "mode": "write",
                        "content": "# Current Run\n\nNot enough.\n",
                    }
                ],
            },
            snapshot,
            {},
        )

        self.assertIn("rewrite STRATEGY.md", error)

    def test_game_over_lifecycle_prompt_warns_current_run_will_be_cleared(self):
        snapshot = {
            "state": {"state_type": "game_over"},
            "actions": [{"id": "menu:main_menu"}],
            "progress_update": {"last_completed_victory": False},
        }

        requirement = orchestrator.build_lifecycle_requirement(snapshot, {})

        self.assertIn("comprehensive lessons from the completed run", requirement)
        self.assertIn("only persistent cross-run playbook", requirement)
        self.assertIn("do not make a sparse summary", requirement)
        self.assertIn("same prompt's `Memory` section", requirement)
        self.assertIn("whether it won or died", requirement)
        self.assertIn("clear `CURRENT_RUN.md`", requirement)
        self.assertIn("must stand on its own", requirement)

    def test_victory_requires_strategy_rewrite_before_advancing(self):
        snapshot = {
            "state": {"state_type": "game_over"},
            "actions": [{"id": "menu:main_menu"}],
            "progress_update": {"last_completed_victory": True},
        }

        error = orchestrator.validate_memory_lifecycle(
            {"action_ref": "menu:main_menu"}, snapshot, {}
        )

        self.assertIn("rewrite STRATEGY.md", error)

    def test_game_over_accepts_strategy_rewrite(self):
        snapshot = {
            "state": {"state_type": "game_over"},
            "actions": [{"id": "menu:main_menu"}],
            "progress_update": {"last_completed_victory": False},
        }

        error = orchestrator.validate_memory_lifecycle(
            {
                "action_ref": "menu:main_menu",
                "memory_updates": [
                    {
                        "path": "STRATEGY.md",
                        "mode": "write",
                        "content": "# Strategy\n\nAvoid the line that died.\n",
                    }
                ],
            },
            snapshot,
            {},
        )

        self.assertIsNone(error)

    def test_short_current_run_rewrite_is_accepted(self):
        error = orchestrator.validate_memory_quality(
            {
                "action_ref": "0",
                "memory_updates": [
                    {
                        "path": "CURRENT_RUN.md",
                        "mode": "write",
                        "content": "# Current Run\n\nToo short.\n",
                    }
                ],
            },
            {"state": {"state_type": "map"}},
            {
                "CURRENT_RUN.md": "# Current Run\n\n"
                + ("Existing useful context.\n" * 90)
            },
        )

        self.assertIsNone(error)

    def test_short_strategy_rewrite_is_accepted(self):
        error = orchestrator.validate_memory_quality(
            {
                "action_ref": "0",
                "memory_updates": [
                    {
                        "path": "STRATEGY.md",
                        "mode": "write",
                        "content": "# Strategy Memory\n\nOne lesson.\n",
                    }
                ],
            },
            {"state": {"state_type": "game_over"}},
            {"STRATEGY.md": "# Strategy Memory\n\n"},
        )

        self.assertIsNone(error)

    def test_comprehensive_memory_rewrite_is_accepted(self):
        long_current_run = (
            "# Current Run\n\n"
            "## State\n"
            + ("Deck, relic, potion, path, risk, and tactic detail.\n" * 35)
        )

        error = orchestrator.validate_memory_quality(
            {
                "action_ref": "0",
                "memory_updates": [
                    {
                        "path": "CURRENT_RUN.md",
                        "mode": "write",
                        "content": long_current_run,
                    }
                ],
            },
            {"state": {"state_type": "map"}},
            {"CURRENT_RUN.md": "# Current Run\n\nExisting full run state.\n"},
        )

        self.assertIsNone(error)

    def test_build_prompt_does_not_warn_when_persistent_memory_is_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "prompt.md"
            template.write_text(
                "Memory:\n{{MEMORY_JSON}}\nSnapshot:\n{{SNAPSHOT_JSON}}\n",
                encoding="utf-8",
            )

            prompt = orchestrator.build_prompt(
                str(template),
                {"state": {"state_type": "map"}, "actions": []},
                {
                    "CURRENT_RUN.md": "# Current Run\n\nShort.\n",
                    "STRATEGY.md": "# Strategy Memory\n\nShort.\n",
                },
            )

        self.assertNotIn("Memory quality requirement:", prompt)
        self.assertNotIn("currently sparse", prompt)
        self.assertNotIn("at least 1400 chars", prompt)
        self.assertNotIn("at least 1200 chars", prompt)

    def test_build_prompt_inserts_full_memory_files_into_same_prompt(self):
        current_run = "# Current Run\n\nFull active run state.\n"
        strategy = "# Strategy Memory\n\nFull durable playbook.\n"
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "prompt.md"
            template.write_text(
                "Memory:\n{{MEMORY_JSON}}\nSnapshot:\n{{SNAPSHOT_JSON}}\n",
                encoding="utf-8",
            )

            prompt = orchestrator.build_prompt(
                str(template),
                {"state": {"state_type": "map"}, "actions": []},
                {
                    "CURRENT_RUN.md": current_run,
                    "STRATEGY.md": strategy,
                },
            )

        self.assertIn('"CURRENT_RUN.md"', prompt)
        self.assertIn("Full active run state.", prompt)
        self.assertIn('"STRATEGY.md"', prompt)
        self.assertIn("Full durable playbook.", prompt)

    def test_refinement_prompt_contains_full_memory_files_in_same_prompt(self):
        current_run = "# Current Run\n\nFull run details before reward.\n"
        battle_log = "# Battle Log\n\nFull battle tactics to fold in.\n"
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "prompt.md"
            template.write_text(
                "Memory:\n{{MEMORY_JSON}}\nSnapshot:\n{{SNAPSHOT_JSON}}\n",
                encoding="utf-8",
            )

            prompt = orchestrator.build_prompt(
                str(template),
                {
                    "state": {
                        "state_type": "rewards",
                        "battle": {"enemies": [{"name": "Jaw Worm", "hp": 0}]},
                    },
                    "actions": [{"id": "0"}],
                },
                {
                    "CURRENT_RUN.md": current_run,
                    "BATTLE_LOG.md": battle_log,
                    "STRATEGY.md": "# Strategy Memory\n\nDurable lessons.\n",
                },
            )

        self.assertIn("Lifecycle requirement:", prompt)
        self.assertIn("rewrite `CURRENT_RUN.md`", prompt)
        self.assertIn('"CURRENT_RUN.md"', prompt)
        self.assertIn("Full run details before reward.", prompt)
        self.assertIn('"BATTLE_LOG.md"', prompt)
        self.assertIn("Full battle tactics to fold in.", prompt)
        self.assertIn("same prompt's `Memory` section", prompt)
        self.assertLess(prompt.index("Lifecycle requirement:"), prompt.rindex("Snapshot:"))

    def test_build_prompt_keeps_snapshot_and_objective_battle_log_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "prompt.md"
            template.write_text(
                "Memory:\n{{MEMORY_JSON}}\nSnapshot:\n{{SNAPSHOT_JSON}}\n",
                encoding="utf-8",
            )

            prompt = orchestrator.build_prompt(
                str(template),
                {
                    "state": {"state_type": "monster"},
                    "actions": [{"id": "0"}],
                    "objective_battle_log": [
                        {"action": "Bash", "before": {"energy": 3}}
                    ],
                },
                {"BATTLE_LOG.md": "# Battle Log\n\nActive notes.\n"},
            )

        self.assertLess(prompt.index("Memory:"), prompt.index("Snapshot:"))
        snapshot_text = prompt.split("Snapshot:", 1)[1].split(
            "Objective battle log:", 1
        )[0]
        self.assertNotIn("objective_battle_log", snapshot_text)
        self.assertIn('"state_type": "monster"', snapshot_text)
        self.assertTrue(
            prompt.rstrip().endswith(
                '"before": {\n'
                '      "energy": 3\n'
                "    }\n"
                "  }\n"
                "]"
            )
        )

    def test_agent_context_routes_active_battle_to_battle_tactician(self):
        role = orchestrator.context_role_for_snapshot(
            {
                "state": {
                    "state_type": "monster",
                    "battle": {"is_play_phase": True, "turn": "player"},
                },
                "actions": [{"id": "0"}],
            },
            {},
        )

        self.assertEqual(role, "battle_tactician")

    def test_agent_context_keeps_finished_battle_with_tactician_until_folded(self):
        role = orchestrator.context_role_for_snapshot(
            {
                "state": {
                    "state_type": "rewards",
                    "battle": {"enemies": [{"name": "Jaw Worm", "hp": 0}]},
                },
                "actions": [{"id": "0"}],
            },
            {"BATTLE_LOG.md": "# Battle Log\n\nWon with Bash line.\n"},
        )

        self.assertEqual(role, "battle_tactician")

    def test_agent_context_routes_map_to_map_pather(self):
        role = orchestrator.context_role_for_snapshot(
            {"state": {"state_type": "map"}, "actions": [{"id": "map:0"}]},
            {"BATTLE_LOG.md": orchestrator.MEMORY_FILE_TEMPLATES["BATTLE_LOG.md"]},
        )

        self.assertEqual(role, "map_pather")

    def test_agent_context_compaction_requires_role_memory_rewrite(self):
        manager = orchestrator.AgentContextManager(
            orchestrator.AgentContextConfig(
                enabled=True,
                compaction_token_threshold=1,
            )
        )

        prompt, exchange_prompt, conversation, context = manager.build_prompt(
            "Memory:\n{}\nSnapshot:\n{}", "battle_tactician"
        )

        self.assertIn("Context compaction requirement:", prompt)
        self.assertIn("Context compaction requirement:", exchange_prompt)
        self.assertLess(
            prompt.index("Context compaction requirement:"), prompt.rindex("Snapshot:")
        )
        self.assertLess(
            exchange_prompt.index("Context compaction requirement:"),
            exchange_prompt.rindex("Snapshot:"),
        )
        self.assertEqual(context["compaction_path"], "BATTLE_LOG.md")
        error = orchestrator.validate_context_compaction(
            {"action_ref": "0"}, conversation
        )
        self.assertIn("rewriting BATTLE_LOG.md", error)

        accepted = orchestrator.validate_context_compaction(
            {
                "action_ref": "0",
                "memory_updates": [
                    {
                        "path": "BATTLE_LOG.md",
                        "mode": "write",
                        "content": "# Battle Log\n\nCompact state.\n",
                    }
                ],
            },
            conversation,
        )
        self.assertIsNone(accepted)

    def test_agent_context_prompt_order_is_role_history_memory_snapshot(self):
        manager = orchestrator.AgentContextManager(orchestrator.AgentContextConfig(enabled=True))
        conversation = manager.conversation_for("map_pather")
        conversation.append_exchange(
            "old prompt",
            orchestrator.ModelCompletion(
                decision={"action_ref": "0"},
                raw_response={},
                response_text='{"action_ref":"0"}',
                request_payload={},
            ),
            {"status": "ok"},
        )

        prompt, _, _, _ = manager.build_prompt(
            "System instructions.\n\nMemory:\n\n{\"STRATEGY.md\":\"x\"}\n\nSnapshot:\n\n{\"actions\":[]}",
            "map_pather",
        )

        role_index = prompt.index("You are the outer map pather")
        history_index = prompt.index("Agent history:")
        current_prompt_index = prompt.index("Current decision prompt:")
        memory_index = prompt.index("Memory:")
        snapshot_index = prompt.index("Snapshot:")
        self.assertLess(role_index, memory_index)
        self.assertLess(role_index, history_index)
        self.assertLess(history_index, current_prompt_index)
        self.assertLess(current_prompt_index, memory_index)
        self.assertLess(memory_index, snapshot_index)

    def test_build_prompt_preserves_memory_order_for_prompt_cache_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = Path(tmp) / "prompt.md"
            template.write_text(
                "Instructions.\n\nMemory:\n\n{{MEMORY_JSON}}\n\nSnapshot:\n\n{{SNAPSHOT_JSON}}\n",
                encoding="utf-8",
            )

            prompt = orchestrator.build_prompt(
                str(template),
                {"actions": []},
                {
                    "STRATEGY.md": "durable",
                    "CURRENT_RUN.md": "run",
                    "BATTLE_LOG.md": "volatile",
                },
            )

        self.assertLess(prompt.index('"STRATEGY.md"'), prompt.index('"CURRENT_RUN.md"'))
        self.assertLess(prompt.index('"CURRENT_RUN.md"'), prompt.index('"BATTLE_LOG.md"'))
        self.assertLess(prompt.index('"BATTLE_LOG.md"'), prompt.index("Snapshot:"))

    def test_map_pather_memory_bundle_excludes_battle_log(self):
        filtered = orchestrator.memory_bundle_for_context_role(
            {
                "STRATEGY.md": "strategy",
                "CURRENT_RUN.md": "run",
                "BATTLE_LOG.md": "battle",
                "HARNESS_BUGS.md": "bug",
            },
            "map_pather",
        )

        self.assertEqual(
            filtered,
            {
                "STRATEGY.md": "strategy",
                "CURRENT_RUN.md": "run",
            },
        )

    def test_agent_context_reset_all_clears_both_sub_agent_histories(self):
        manager = orchestrator.AgentContextManager(orchestrator.AgentContextConfig(enabled=True))
        completion = orchestrator.ModelCompletion(
            decision={"action_ref": "0"},
            raw_response={},
            response_text='{"action_ref":"0"}',
            request_payload={},
        )
        manager.conversation_for("map_pather").append_exchange(
            "map prompt", completion, {"status": "ok"}
        )
        manager.conversation_for("battle_tactician").append_exchange(
            "battle prompt", completion, {"status": "ok"}
        )

        manager.reset_all()

        self.assertEqual(manager.conversation_for("map_pather").exchanges, [])
        self.assertEqual(manager.conversation_for("battle_tactician").exchanges, [])

    def test_restore_agent_context_from_trace_rebuilds_non_recursive_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sqlite_path = root / "runs.sqlite"
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute(
                    """
                    create table steps (
                        id integer primary key,
                        run_id text,
                        prompt_text text,
                        response_text text,
                        action_chosen text,
                        invalid_action_error text,
                        observation_json text,
                        state_type text,
                        floor integer,
                        hp integer,
                        max_hp integer,
                        gold integer
                    )
                    """
                )
                prompt = (
                    "Agent role: battle_tactician\n"
                    "Prompt-cache hint: old.\n\n"
                    "You are the battle tactician sub-agent for the current battle.\n\n"
                    "Memory:\n\n{\"BATTLE_LOG.md\":\"now\"}"
                    "\n\nAgent history:\nExchange 1\nPrompt:\nold"
                    "\n\nSnapshot:\n\n{\"actions\":[{\"id\":\"end_turn\"}]}"
                )
                conn.execute(
                    """
                    insert into steps (
                        run_id, prompt_text, response_text, action_chosen,
                        invalid_action_error
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        "old-run",
                        "Agent role: map_pather\nPrompt-cache hint: old.\n\n"
                        "You are the outer map pather and strategic agent.\n\n"
                        "Memory:\n\n{\"STRATEGY.md\":\"old\"}",
                        '{"action_ref":"old","rationale":"old"}',
                        '{"id":"old"}',
                        None,
                    ),
                )
                conn.execute(
                    """
                    insert into steps (
                        run_id, prompt_text, response_text, action_chosen,
                        invalid_action_error
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        "current-run",
                        prompt,
                        '{"action_ref":"end_turn","rationale":"done"}',
                        '{"id":"end_turn"}',
                        None,
                    ),
                )

            pi_config = root / "pi.json"
            harness_config = root / "harness.json"
            harness_config.write_text(
                json.dumps({"logging": {"sqlite_path": str(sqlite_path)}}),
                encoding="utf-8",
            )
            pi_config.write_text(
                json.dumps({"harness_config": str(harness_config)}),
                encoding="utf-8",
            )
            config = orchestrator.OrchestratorConfig(
                rpc_server_command=["python", "pi_agent/rpc_server.py", "--config", str(pi_config)],
                model=orchestrator.ModelConfig(provider="openai_responses", model="x"),
                decision_log=str(root / "decisions.jsonl"),
            )
            manager = orchestrator.AgentContextManager(
                orchestrator.AgentContextConfig(enabled=True)
            )

            record = orchestrator.restore_agent_context_from_trace(manager, config)

        self.assertTrue(record["restored"])
        self.assertEqual(record["run_id"], "current-run")
        self.assertEqual(record["exchanges"], 1)
        exchanges = manager.conversation_for("battle_tactician").exchanges
        self.assertEqual(len(exchanges), 1)
        self.assertIn("Restored historical decision from trace", exchanges[0]["prompt"])
        self.assertNotIn("Memory:", exchanges[0]["prompt"])
        self.assertNotIn("Snapshot:", exchanges[0]["prompt"])
        self.assertNotIn("Agent history:", exchanges[0]["prompt"])

    def test_current_run_rewrite_out_of_combat_clears_battle_log(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))

        rpc = FakeRpc()

        records = orchestrator.enforce_memory_lifecycle(
            rpc,
            {
                "state": {
                    "state_type": "rewards",
                    "battle": {"enemies": [{"name": "Jaw Worm", "hp": 0}]},
                }
            },
            {"CURRENT_RUN.md"},
        )

        self.assertEqual(
            rpc.calls,
            [
                (
                    "write_memory",
                    {
                        "path": "BATTLE_LOG.md",
                        "content": orchestrator.MEMORY_FILE_TEMPLATES[
                            "BATTLE_LOG.md"
                        ],
                    },
                )
            ],
        )
        self.assertEqual(
            records,
            [
                {
                    "path": "BATTLE_LOG.md",
                    "reason": "current_run_rewritten_after_battle",
                }
            ],
        )

    def test_current_run_rewrite_in_combat_keeps_battle_log(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))

        rpc = FakeRpc()

        records = orchestrator.enforce_memory_lifecycle(
            rpc,
            {"state": {"state_type": "monster"}},
            {"CURRENT_RUN.md"},
        )

        self.assertEqual(rpc.calls, [])
        self.assertEqual(records, [])

    def test_current_run_rewrite_in_combat_hand_select_keeps_battle_log(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))

        rpc = FakeRpc()

        records = orchestrator.enforce_memory_lifecycle(
            rpc,
            {
                "state": {
                    "state_type": "hand_select",
                    "battle": {"is_play_phase": True, "turn": "player"},
                }
            },
            {"CURRENT_RUN.md"},
        )

        self.assertEqual(rpc.calls, [])
        self.assertEqual(records, [])

    def test_strategy_rewrite_after_completed_run_resets_run_notes(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))

        rpc = FakeRpc()

        records = orchestrator.enforce_memory_lifecycle(
            rpc,
            {
                "state": {"state_type": "game_over"},
                "progress_update": {"last_completed_victory": False},
            },
            {"STRATEGY.md"},
        )

        self.assertEqual(
            rpc.calls,
            [
                (
                    "write_memory",
                    {
                        "path": "CURRENT_RUN.md",
                        "content": orchestrator.MEMORY_FILE_TEMPLATES[
                            "CURRENT_RUN.md"
                        ],
                    },
                ),
                (
                    "write_memory",
                    {
                        "path": "BATTLE_LOG.md",
                        "content": orchestrator.MEMORY_FILE_TEMPLATES[
                            "BATTLE_LOG.md"
                        ],
                    },
                ),
            ],
        )
        self.assertEqual(
            records,
            [
                {
                    "path": "CURRENT_RUN.md",
                    "reason": "strategy_rewritten_after_completed_run",
                },
                {
                    "path": "BATTLE_LOG.md",
                    "reason": "strategy_rewritten_after_completed_run",
                },
            ],
        )

    def test_strategy_rewrite_after_victory_resets_run_notes(self):
        class FakeRpc:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None):
                self.calls.append((method, params))

        rpc = FakeRpc()

        records = orchestrator.enforce_memory_lifecycle(
            rpc,
            {
                "state": {"state_type": "game_over"},
                "progress_update": {"last_completed_victory": True},
            },
            {"STRATEGY.md", "CURRENT_RUN.md"},
        )

        self.assertEqual(
            rpc.calls,
            [
                (
                    "write_memory",
                    {
                        "path": "CURRENT_RUN.md",
                        "content": orchestrator.MEMORY_FILE_TEMPLATES[
                            "CURRENT_RUN.md"
                        ],
                    },
                ),
                (
                    "write_memory",
                    {
                        "path": "BATTLE_LOG.md",
                        "content": orchestrator.MEMORY_FILE_TEMPLATES[
                            "BATTLE_LOG.md"
                        ],
                    },
                ),
            ],
        )
        self.assertEqual(
            [record["path"] for record in records],
            ["CURRENT_RUN.md", "BATTLE_LOG.md"],
        )

    def test_next_decision_step_resumes_existing_jsonl_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps({"step": 0, "event": "start"}),
                        "not json",
                        json.dumps({"step": 199, "action_ref": "0"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            step = orchestrator.next_decision_step(str(path))

        self.assertEqual(step, 200)


class RunSetupActionTests(unittest.TestCase):
    def test_opencode_trial_seed_file_preserves_existing_valid_prefix(self):
        existing = Path(
            "seed_sets/opencode_go_deepseek_v4_flash_valid_20260607.txt"
        ).read_text(encoding="utf-8").splitlines()
        trial = Path(
            "seed_sets/opencode_go_deepseek_v4_flash_trial_20260618.txt"
        ).read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(existing), 20)
        self.assertEqual(len(trial), 100)
        self.assertEqual(trial[: len(existing)], existing)
        self.assertEqual(len(set(trial)), len(trial))
        for seed in trial:
            self.assertEqual(len(seed), 10)
            self.assertTrue(seed.isalnum())
            self.assertEqual(seed, seed.upper())

    def test_seed_file_loads_ordered_seed_set(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_file = Path(tmpdir) / "seeds.txt"
            seed_file.write_text("# comment\nS1\n\nS2\nS3\n", encoding="utf-8")
            config_file = Path(tmpdir) / "harness.json"
            config_file.write_text(
                json.dumps(
                    {
                        "run_setup": {
                            "seed_policy": "fixed_list_until_win",
                            "seed_file": "seeds.txt",
                            "ascension": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )

            setup = main.load_run_setup(str(config_file))

        self.assertEqual(setup.seed_set, ("S1", "S2", "S3"))
        self.assertEqual(setup.seed, "S1")

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

    def test_game_over_exposes_main_menu_restart_action(self):
        actions = main.build_actions({"state_type": "game_over"})

        self.assertEqual([action.id for action in actions], ["menu:main_menu"])
        self.assertEqual(
            actions[0].request, {"action": "menu_select", "option": "main_menu"}
        )

    def test_auto_resolve_restarts_and_starts_configured_seeded_custom_run(self):
        class FakeActionClient:
            def __init__(self):
                self.requests = []
                self.states = [
                    {
                        "state_type": "menu",
                        "menu_screen": "main_menu",
                        "options": [{"name": "singleplayer", "enabled": True}],
                    },
                    {
                        "state_type": "menu",
                        "menu_screen": "singleplayer",
                        "options": [{"name": "custom", "enabled": True}],
                    },
                    {
                        "state_type": "menu",
                        "menu_screen": "custom_run",
                        "options": [{"name": "IRONCLAD", "enabled": True}],
                    },
                    {
                        "state_type": "menu",
                        "menu_screen": "custom_run",
                        "selected_character": {"id": "IRONCLAD"},
                        "options": [{"name": "confirm", "enabled": True}],
                    },
                    {
                        "state_type": "monster",
                        "battle": {"is_play_phase": True, "turn": "player"},
                    },
                ]

            def post_action(self, request):
                self.requests.append(request)
                return {"status": "ok"}

            def get_state(self, *, response_format="json"):
                del response_format
                return self.states.pop(0)

        old_verify = main.verify_started_run_setup
        try:
            main.verify_started_run_setup = lambda action, setup: {
                "status": "ok",
                "expected": {"seed": setup.seed, "ascension": setup.ascension},
            }
            with tempfile.TemporaryDirectory() as tmpdir:
                config = main.HarnessConfig(
                    run_setup=main.RunSetup(
                        seed="S1",
                        ascension=0,
                        character="IRONCLAD",
                        progress_file=str(Path(tmpdir) / "progress.json"),
                        save_root=tmpdir,
                    ),
                    auto_resolve=True,
                    max_auto_actions=5,
                )
                client = FakeActionClient()

                state, auto_actions = main.resolve_auto_actions(
                    client,
                    {"state_type": "game_over"},
                    config,
                    wait=0,
                )
        finally:
            main.verify_started_run_setup = old_verify

        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(
            client.requests,
            [
                {"action": "menu_select", "option": "main_menu"},
                {"action": "menu_select", "option": "singleplayer"},
                {"action": "menu_select", "option": "custom"},
                {"action": "menu_select", "option": "IRONCLAD"},
                {
                    "action": "menu_select",
                    "option": "confirm",
                    "seed": "S1",
                    "ascension": 0,
                },
            ],
        )
        self.assertEqual(len(auto_actions), 5)
        self.assertEqual(auto_actions[-1]["run_setup_verification"]["status"], "ok")

    def test_auto_resolve_uses_progress_bump_for_next_run_after_victory(self):
        class FakeActionClient:
            def __init__(self):
                self.requests = []
                self.states = [
                    {
                        "state_type": "menu",
                        "menu_screen": "main_menu",
                        "options": [{"name": "singleplayer", "enabled": True}],
                    },
                    {
                        "state_type": "menu",
                        "menu_screen": "singleplayer",
                        "options": [{"name": "custom", "enabled": True}],
                    },
                    {
                        "state_type": "menu",
                        "menu_screen": "custom_run",
                        "options": [{"name": "confirm", "enabled": True}],
                    },
                    {
                        "state_type": "monster",
                        "battle": {"is_play_phase": True, "turn": "player"},
                    },
                ]

            def post_action(self, request):
                self.requests.append(request)
                return {"status": "ok"}

            def get_state(self, *, response_format="json"):
                del response_format
                return self.states.pop(0)

        old_verify = main.verify_started_run_setup
        try:
            main.verify_started_run_setup = lambda action, setup: {
                "status": "ok",
                "expected": {"seed": setup.seed, "ascension": setup.ascension},
            }
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
                            "victory": True,
                        }
                    ),
                    encoding="utf-8",
                )
                config = main.HarnessConfig(
                    run_setup=main.RunSetup(
                        seed="S1",
                        seed_set=("S1", "S2"),
                        seed_policy="fixed_list_until_win",
                        ascension=0,
                        progress_file=progress_file,
                        save_root=tmpdir,
                    ),
                    auto_resolve=True,
                    max_auto_actions=4,
                )
                client = FakeActionClient()

                state, auto_actions = main.resolve_auto_actions(
                    client,
                    {"state_type": "game_over"},
                    config,
                    wait=0,
                )
        finally:
            main.verify_started_run_setup = old_verify

        self.assertEqual(state["state_type"], "monster")
        self.assertEqual(
            client.requests[-1],
            {
                "action": "menu_select",
                "option": "confirm",
                "seed": "S2",
                "ascension": 1,
            },
        )
        self.assertEqual(auto_actions[-1]["run_setup_verification"]["status"], "ok")
        self.assertEqual(
            auto_actions[-1]["run_setup_verification"]["expected"],
            {"seed": "S2", "ascension": 1},
        )


class ActionGenerationTests(unittest.TestCase):
    def test_end_turn_requires_confirmation_with_energy_and_playable_cards(self):
        state = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 1,
                "hand": [{"index": 0, "name": "Defend", "can_play": True}],
            },
        }

        actions = main.build_actions(state)

        self.assertIn("end_turn_confirm", [action.id for action in actions])
        self.assertNotIn("end_turn", [action.id for action in actions])

    def test_end_turn_does_not_require_confirmation_without_playable_cards(self):
        state = {
            "state_type": "monster",
            "battle": {"is_play_phase": True, "turn": "player", "enemies": []},
            "player": {
                "energy": 1,
                "hand": [{"index": 0, "name": "Defend", "can_play": False}],
            },
        }

        actions = main.build_actions(state)

        self.assertIn("end_turn", [action.id for action in actions])

    def test_hand_select_action_labels_selectable_index_mapping(self):
        state = {
            "state_type": "hand_select",
            "hand_select": {
                "cards": [
                    {"index": 0, "name": "Setup Strike"},
                    {"index": 1, "name": "Twin Strike"},
                    {"index": 6, "name": "Howl from Beyond"},
                ],
            },
            "player": {
                "hand": [
                    {"index": 0, "name": "Setup Strike"},
                    {"index": 1, "name": "Twin Strike"},
                    {"index": 7, "name": "Howl from Beyond"},
                ]
            },
        }

        actions = main.build_actions(state)
        howl = next(action for action in actions if action.id == "combat_select_card:6")

        self.assertEqual(
            howl.label, "Select selectable[6] Howl from Beyond (player hand[7])"
        )
        self.assertEqual(
            howl.request, {"action": "combat_select_card", "card_index": 6}
        )
        self.assertIn("maps to player.hand[7]", howl.notes[1])

    def test_hand_select_keeps_combat_potion_use_actions(self):
        state = {
            "state_type": "hand_select",
            "battle": {"is_play_phase": True, "turn": "player"},
            "hand_select": {
                "cards": [{"index": 0, "name": "Strike"}],
                "can_confirm": True,
            },
            "player": {
                "potions": [{"slot": 0, "name": "Flex Potion"}],
            },
        }

        actions = main.build_actions(state)
        action_ids = [action.id for action in actions]

        self.assertIn("use_potion:0", action_ids)
        self.assertIn("discard_potion:0", action_ids)
        self.assertIn("combat_select_card:0", action_ids)

    def test_full_potion_reward_is_visible_but_disabled(self):
        state = {
            "state_type": "rewards",
            "player": {
                "max_potion_slots": 1,
                "potions": [{"slot": 0, "name": "Fire Potion"}],
            },
            "rewards": {
                "items": [{"index": 0, "type": "potion", "potion_name": "Dex Potion"}]
            },
        }

        actions = main.build_actions(state)
        action = next(
            action for action in actions if action.id == "rewards_claim:0:disabled"
        )

        self.assertEqual(action.id, "rewards_claim:0:disabled")
        self.assertFalse(action.enabled)
        self.assertIn("potion slots are full", action.notes[0])
        with self.assertRaisesRegex(ValueError, "disabled"):
            main.find_action(actions, "rewards_claim:0:disabled")

    def test_auto_action_claims_gold_reward_even_with_other_actions(self):
        state = {
            "state_type": "rewards",
            "rewards": {
                "items": [
                    {"index": 0, "type": "gold", "gold_amount": 25},
                    {"index": 1, "type": "relic", "description": "Anchor"},
                ],
                "can_proceed": True,
            },
        }
        actions = main.build_actions(state)

        action = main.auto_action_for_state(state, actions)

        self.assertIsNotNone(action)
        self.assertEqual(action.request, {"action": "claim_reward", "index": 0})

    def test_card_select_confirm_is_hidden_when_cards_are_visible(self):
        state = {
            "state_type": "card_select",
            "card_select": {
                "screen_type": "NDeckEnchantSelectScreen",
                "cards": [{"index": 0, "name": "Bash"}],
                "preview_showing": False,
                "can_confirm": True,
            },
        }

        actions = main.build_actions(state)

        self.assertEqual([action.id for action in actions], ["deck_select_card:0"])

    def test_post_card_select_auto_confirm_stops_on_no_progress(self):
        state = {
            "state_type": "card_select",
            "card_select": {
                "screen_type": "NDeckEnchantSelectScreen",
                "cards": [{"index": 0, "name": "Bash"}],
                "preview_showing": False,
                "can_confirm": True,
            },
        }

        class FakeClient:
            def __init__(self):
                self.requests = []

            def post_action(self, request):
                self.requests.append(request)
                return {"status": "ok", "message": "Confirming selection"}

            def get_state(self, *, response_format="json"):
                del response_format
                return state

        previous_action = main.Action(
            id="deck_select_card:0",
            label="Select NDeckEnchantSelectScreen card[0] Bash",
            category="card_select",
            request={"action": "select_card", "index": 0},
        )
        client = FakeClient()

        after, auto_actions = main.resolve_auto_actions(
            client,
            state,
            main.HarnessConfig(
                run_setup=main.RunSetup(), auto_resolve=True, max_auto_actions=5
            ),
            wait=0,
            previous_action=previous_action,
        )

        self.assertIs(after, state)
        self.assertEqual(client.requests, [{"action": "confirm_selection"}])
        self.assertEqual(len(auto_actions), 1)
        self.assertEqual(
            auto_actions[0]["action"]["request"], {"action": "confirm_selection"}
        )
        self.assertTrue(auto_actions[0]["no_progress"])


class ProgressTests(unittest.TestCase):
    def test_history_win_increments_ascension_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_file = str(Path(tmpdir) / "progress.json")
            history = (
                Path(tmpdir) / "steamid" / "modded" / "profile1" / "saves" / "history"
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

    def test_fixed_list_until_win_retries_same_seed_after_death(self):
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
                seed_set=("S1", "S2", "S3"),
                ascension=0,
                progress_file=progress_file,
                save_root=tmpdir,
                seed_policy="fixed_list_until_win",
            )

            update = main.maybe_update_progress_after_state(
                FakeClient([]), {"state_type": "game_over"}, setup
            )

            self.assertEqual(update["next_seed_index"], 0)
            progress = main._load_json_file(progress_file)
            self.assertNotIn("current_seed_index", progress)
            self.assertEqual(main.current_seed_for_setup(setup, progress), "S1")

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
            subprocess.run(
                ["git", "init"], cwd=tmpdir, check=True, stdout=subprocess.PIPE
            )
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

    def test_ensure_memory_git_worktree_adds_condition_worktree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "memory-source"
            worktree = Path(tmpdir) / "condition-worktree"
            source.mkdir()
            subprocess.run(["git", "init"], cwd=source, check=True, stdout=subprocess.PIPE)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=source,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "initial memory repo"],
                cwd=source,
                check=True,
                stdout=subprocess.PIPE,
            )
            config = main.HarnessConfig(
                run_setup=main.RunSetup(),
                agent=main.AgentConfig(
                    agent_name="pi-agent",
                    model_name="model",
                    condition_name="memory",
                ),
                logging=main.LoggingConfig(
                    memory_git_source=str(source),
                    memory_git_dir=str(worktree),
                    memory_git_branch="memory/test-condition",
                ),
            )

            result = main.ensure_memory_git_worktree(config)

            self.assertEqual(result["status"], "created")
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=worktree,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(branch, "memory/test-condition")

    def test_room_checkpoint_commits_once_per_floor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = str(Path(tmpdir) / "runs.sqlite")
            progress_file = str(Path(tmpdir) / "progress.json")
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
                    memory_git_dir=tmpdir,
                    memory_commit_on_room_change=True,
                    memory_commit_paths=(
                        "STRATEGY.md",
                        "CURRENT_RUN.md",
                        "BATTLE_LOG.md",
                    ),
                ),
            )
            main.start_logged_run(config, {"state_type": "monster"}, None)

            first = main.maybe_commit_memory_checkpoint(
                config, {"state_type": "monster", "floor": 1}, reason="room"
            )
            second = main.maybe_commit_memory_checkpoint(
                config, {"state_type": "monster", "floor": 1}, reason="room"
            )
            Path(tmpdir, "BATTLE_LOG.md").write_text("floor 2\n", encoding="utf-8")
            third = main.maybe_commit_memory_checkpoint(
                config, {"state_type": "monster", "floor": 2}, reason="room"
            )

            self.assertEqual(first["status"], "committed")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(third["status"], "committed")
            count = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=tmpdir,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            self.assertEqual(count, "2")

    def test_run_end_checkpoint_commits_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = str(Path(tmpdir) / "runs.sqlite")
            progress_file = str(Path(tmpdir) / "progress.json")
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
            Path(tmpdir, "STRATEGY.md").write_text("updated\n", encoding="utf-8")
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
                    memory_git_dir=tmpdir,
                    memory_commit_on_run_end=True,
                    memory_commit_paths=("STRATEGY.md",),
                ),
            )
            main.start_logged_run(config, {"state_type": "monster"}, None)

            result = main.maybe_commit_memory_checkpoint(
                config, {"state_type": "game_over", "floor": 12}, reason="run_end"
            )

            self.assertEqual(result["status"], "committed")
            log = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=tmpdir,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertIn("memory run-end snapshot", log)


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
                """
                SELECT action_source, action_chosen, invalid_action_ref,
                       invalid_action_error
                FROM steps WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            conn.close()
            self.assertEqual(run_row[0], 1)
            self.assertEqual(step_row[0], "invalid_agent")
            self.assertIn("play_card:7", step_row[1])
            self.assertEqual(step_row[2], "play_card:7")
            self.assertEqual(step_row[3], "not legal")

    def test_objective_battle_log_summarizes_prior_observed_actions(self):
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
                "floor": 14,
                "battle": {
                    "is_play_phase": True,
                    "turn": "player",
                    "enemies": [{"entity_id": "CULTIST_0", "name": "Cultist", "hp": 20}],
                },
                "player": {
                    "hp": 28,
                    "max_hp": 80,
                    "energy": 3,
                    "max_energy": 3,
                    "hand": [
                        {"index": 0, "name": "Strike", "can_play": True},
                        {"index": 1, "name": "Defend", "can_play": True},
                    ],
                    "draw_pile_count": 10,
                    "discard_pile_count": 0,
                },
            }
            action = main.Action(
                id="play_card:0:cultist_0",
                label="Play hand[0] Strike on Cultist",
                category="combat",
                request={
                    "action": "play_card",
                    "card_index": 0,
                    "target": "CULTIST_0",
                },
            )
            main.start_logged_run(config, state, None)
            main.log_step(config, state, [action], action, action_source="agent")

            current = {
                **state,
                "player": {
                    **state["player"],
                    "energy": 2,
                    "hand": [{"index": 0, "name": "Defend", "can_play": True}],
                    "discard_pile_count": 1,
                },
            }
            log = main.build_objective_battle_log(config, current)

        self.assertIsNotNone(log)
        assert log is not None
        self.assertIn("state before the listed action", log["note"])
        self.assertEqual(log["current_observed_state"]["energy"], 2)
        self.assertEqual(log["entries"][0]["energy"], 3)
        self.assertEqual(log["entries"][0]["hand_count"], 2)
        self.assertEqual(
            log["entries"][0]["action_chosen"]["id"], "play_card:0:cultist_0"
        )

    def test_finalize_logged_run_fills_history_fields_and_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sqlite_path = str(Path(tmpdir) / "runs.sqlite")
            progress_file = str(Path(tmpdir) / "progress.json")
            history = Path(tmpdir) / "saves" / "history"
            history.mkdir(parents=True)
            (history / "run1.run").write_text(
                json.dumps(
                    {
                        "start_time": 123,
                        "seed": "S1",
                        "ascension": 0,
                        "win": False,
                        "acts": ["ACT.ONE", "ACT.TWO"],
                        "killed_by_encounter": "ENCOUNTER.TEST_BOSS",
                        "killed_by_event": "NONE.NONE",
                        "map_point_history": [
                            [
                                {
                                    "map_point_type": "monster",
                                    "rooms": [{"room_type": "monster"}],
                                }
                            ],
                            [
                                {
                                    "map_point_type": "boss",
                                    "rooms": [
                                        {
                                            "room_type": "boss",
                                            "model_id": "ENCOUNTER.TEST_BOSS",
                                        }
                                    ],
                                }
                            ],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = main.HarnessConfig(
                run_setup=main.RunSetup(
                    seed="S1",
                    ascension=0,
                    progress_file=progress_file,
                    save_root=tmpdir,
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
            start = main.start_logged_run(config, {"state_type": "monster"}, None)

            main.finalize_logged_run(config, {"state_type": "game_over"})

            conn = sqlite3.connect(sqlite_path)
            run_row = conn.execute(
                """
                SELECT final_floor, act, boss_reached, victory, death_reason
                FROM runs WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            summary_row = conn.execute(
                """
                SELECT harness_summary FROM run_summaries WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            conn.close()

            self.assertEqual(run_row, (2, 2, 1, 0, "ENCOUNTER.TEST_BOSS"))
            self.assertIsNotNone(summary_row)
            self.assertIn("Run ended in loss", summary_row[0])
            self.assertIn("boss reached", summary_row[0])


class ModelTelemetryLoggingTests(unittest.TestCase):
    def test_record_model_telemetry_updates_latest_agent_step_and_run_totals(self):
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
            action = main.Action(
                id="end_turn",
                label="End turn",
                category="combat",
                request={"action": "end_turn"},
            )
            start = main.start_logged_run(config, state, None)
            main.log_step(config, state, [action], action, action_source="agent")

            result = main.record_model_telemetry(
                config,
                prompt_hash="prompt-hash",
                response_hash="response-hash",
                prompt_text="full prompt",
                response_text="full response",
                raw_response={
                    "id": "gen-1",
                    "choices": [
                        {
                            "message": {
                                "content": "full response",
                                "reasoning_content": "reasoning trace",
                            }
                        }
                    ],
                },
                request_payload={"model": "model"},
                provider_name="OpenRouter",
                request_id="req-1",
                response_id="gen-1",
                generation_id="gen-1",
                upstream_id="chatcmpl-1",
                total_cost=0.012,
                prompt_cost=0.004,
                completion_cost=0.008,
                native_tokens_prompt=100,
                native_tokens_completion=50,
                generation_stats={"data": {"total_cost": 0.012}},
                generation_content={"data": {"output": {"completion": "full response"}}},
                input_tokens=123,
                output_tokens=45,
                model_elapsed_seconds=3.0,
                tool_calls={
                    "snapshot": 1,
                    "memory_reads": 4,
                    "memory_writes": 2,
                    "act": 1,
                },
            )

            self.assertEqual(result["status"], "recorded")
            conn = sqlite3.connect(sqlite_path)
            run_row = conn.execute(
                """
                SELECT total_model_calls, total_input_tokens, total_output_tokens,
                       total_tool_calls, total_cost, total_prompt_cost,
                       total_completion_cost, total_native_tokens_prompt,
                       total_native_tokens_completion, total_model_elapsed_seconds
                FROM runs WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            step_row = conn.execute(
                """
                SELECT prompt_hash, response_hash, tool_calls, prompt_text,
                       response_text, raw_response_json, request_json,
                       provider_name, request_id, response_id, generation_id,
                       upstream_id, total_cost, prompt_cost, completion_cost,
                       native_tokens_prompt, native_tokens_completion,
                       model_elapsed_seconds, generation_stats_json,
                       generation_content_json
                FROM steps WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            conn.close()

            self.assertEqual(run_row[:4], (1, 123, 45, 8))
            self.assertAlmostEqual(run_row[4], 0.012)
            self.assertAlmostEqual(run_row[5], 0.004)
            self.assertAlmostEqual(run_row[6], 0.008)
            self.assertEqual(run_row[7], 100)
            self.assertEqual(run_row[8], 50)
            self.assertAlmostEqual(run_row[9], 3.0)
            self.assertEqual(step_row[0], "prompt-hash")
            self.assertEqual(step_row[1], "response-hash")
            self.assertIn('"memory_writes": 2', step_row[2])
            self.assertEqual(step_row[3], "full prompt")
            self.assertEqual(step_row[4], "full response")
            self.assertIn('"id": "gen-1"', step_row[5])
            self.assertIn('"reasoning_content": "reasoning trace"', step_row[5])
            self.assertIn('"model": "model"', step_row[6])
            self.assertEqual(step_row[7], "OpenRouter")
            self.assertEqual(step_row[8], "req-1")
            self.assertEqual(step_row[9], "gen-1")
            self.assertEqual(step_row[10], "gen-1")
            self.assertEqual(step_row[11], "chatcmpl-1")
            self.assertAlmostEqual(step_row[12], 0.012)
            self.assertAlmostEqual(step_row[13], 0.004)
            self.assertAlmostEqual(step_row[14], 0.008)
            self.assertEqual(step_row[15], 100)
            self.assertEqual(step_row[16], 50)
            self.assertAlmostEqual(step_row[17], 3.0)
            self.assertIn('"total_cost": 0.012', step_row[18])
            self.assertIn('"completion": "full response"', step_row[19])

    def test_record_model_telemetry_can_attach_to_invalid_agent_step(self):
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
            state = {"state_type": "map"}
            start = main.start_logged_run(config, state, None)
            main.log_invalid_action(config, state, [], "bad_action", "not legal")

            result = main.record_model_telemetry(
                config,
                prompt_hash="prompt-hash",
                response_hash="response-hash",
                input_tokens=10,
                output_tokens=4,
                tool_calls={"total": 5},
            )

            self.assertEqual(result["status"], "recorded")
            conn = sqlite3.connect(sqlite_path)
            row = conn.execute(
                """
                SELECT action_source, prompt_hash, response_hash
                FROM steps WHERE run_id = ?
                """,
                (start["run_id"],),
            ).fetchone()
            conn.close()
            self.assertEqual(row, ("invalid_agent", "prompt-hash", "response-hash"))


if __name__ == "__main__":
    unittest.main()
