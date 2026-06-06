from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HARNESS_ROOT = Path(__file__).resolve().parents[1]
if str(HARNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(HARNESS_ROOT))

import main as harness  # noqa: E402


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PiAgentConfig:
    harness_config: str
    memory_root: str
    base_url: str = harness.DEFAULT_BASE_URL
    timeout: float = 30.0
    mcp_delay: float = harness.DEFAULT_MCP_DELAY
    wait_after_action: float = 2.0


def _load_json(path: str) -> JsonDict:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_pi_config(path: str) -> PiAgentConfig:
    config = _load_json(path)
    return PiAgentConfig(
        harness_config=str(config["harness_config"]),
        memory_root=str(config.get("memory_root") or "pi_agent/memory"),
        base_url=str(config.get("base_url") or harness.DEFAULT_BASE_URL),
        timeout=float(config.get("timeout", 30.0)),
        mcp_delay=float(config.get("mcp_delay", harness.DEFAULT_MCP_DELAY)),
        wait_after_action=float(config.get("wait_after_action", 2.0)),
    )


class PiRpcServer:
    def __init__(self, config: PiAgentConfig) -> None:
        self.config = config
        self.memory_root = Path(config.memory_root).expanduser().resolve()
        self.memory_root.mkdir(parents=True, exist_ok=True)
        self.harness_config = harness.load_harness_config(config.harness_config)
        self.client = harness.Sts2Client(
            config.base_url,
            timeout=config.timeout,
            mcp_delay=config.mcp_delay,
        )

    def dispatch(self, method: str, params: JsonDict) -> Any:
        methods = {
            "ping": self.ping,
            "snapshot": self.snapshot,
            "actions": self.actions,
            "act": self.act,
            "raw_state": self.raw_state,
            "list_memory": self.list_memory,
            "read_memory": self.read_memory,
            "write_memory": self.write_memory,
            "append_memory": self.append_memory,
        }
        handler = methods.get(method)
        if handler is None:
            raise ValueError(f"unknown method: {method}")
        return handler(params)

    def ping(self, params: JsonDict) -> JsonDict:
        del params
        return {"status": "ok", "harness_root": str(HARNESS_ROOT)}

    def raw_state(self, params: JsonDict) -> Any:
        response_format = str(params.get("format") or "json")
        return self.client.get_state(response_format=response_format)

    def actions(self, params: JsonDict) -> JsonDict:
        del params
        state = harness._wait_for_play_phase(self.client)
        state, auto_actions = harness.resolve_auto_actions(
            self.client, state, self.harness_config
        )
        actions = harness.build_actions(state, self.harness_config.run_setup)
        output: JsonDict = {
            "state_type": state.get("state_type"),
            "actions": harness.action_dicts(actions),
        }
        if auto_actions:
            output["auto_actions"] = auto_actions
        return output

    def snapshot(self, params: JsonDict) -> JsonDict:
        del params
        state = harness._wait_for_play_phase(self.client)
        state, auto_actions = harness.resolve_auto_actions(
            self.client, state, self.harness_config
        )
        actions = harness.build_actions(state, self.harness_config.run_setup)
        output: JsonDict = {
            "state": state,
            "actions": harness.action_dicts(actions),
        }
        if auto_actions:
            output["auto_actions"] = auto_actions
        progress_update = harness.maybe_update_progress_after_state(
            self.client, state, self.harness_config.run_setup
        )
        if progress_update is not None:
            output["progress_update"] = progress_update
        harness.finalize_logged_run(self.harness_config, state)
        return output

    def act(self, params: JsonDict) -> JsonDict:
        if "action" not in params:
            raise ValueError("act requires an action index or ID in params.action")
        action_ref = str(params["action"])
        before = harness._wait_for_play_phase(self.client)
        before, pre_auto_actions = harness.resolve_auto_actions(
            self.client, before, self.harness_config
        )
        actions = harness.build_actions(before, self.harness_config.run_setup)
        action = harness.find_action(actions, action_ref)
        start_action = (
            action.request.get("action") == "menu_select"
            and str(action.request.get("option") or "").lower() in {"confirm", "embark"}
        )
        if not start_action:
            harness.log_step(
                self.harness_config, before, actions, action, action_source="agent"
            )
        result = self.client.post_action(action.request)
        wait = float(params.get("wait", self.config.wait_after_action))
        output: JsonDict = {
            "action": action.as_dict(actions.index(action)),
            "result": result,
        }
        if pre_auto_actions:
            output["pre_auto_actions"] = pre_auto_actions
        if params.get("no_after") is True:
            return output
        if wait > 0:
            time.sleep(wait)
        after = harness._wait_for_play_phase(self.client)
        after, post_auto_actions = harness.resolve_auto_actions(
            self.client, after, self.harness_config
        )
        output["state"] = after
        output["actions"] = harness.action_dicts(
            harness.build_actions(after, self.harness_config.run_setup)
        )
        if post_auto_actions:
            output["auto_actions"] = post_auto_actions
        progress_update = harness.maybe_update_progress_after_state(
            self.client, after, self.harness_config.run_setup
        )
        if progress_update is not None:
            output["progress_update"] = progress_update
        harness.finalize_logged_run(self.harness_config, after)
        verification = harness.verify_started_run_setup(
            action, self.harness_config.run_setup
        )
        if verification is not None:
            output["run_setup_verification"] = verification
            log_start = harness.start_logged_run(self.harness_config, after, verification)
            if log_start is not None:
                output["run_log"] = log_start
                harness.log_step(
                    self.harness_config,
                    before,
                    actions,
                    action,
                    action_source="agent",
                )
        return output

    def list_memory(self, params: JsonDict) -> JsonDict:
        relative = str(params.get("path") or ".")
        root = self._safe_path(relative)
        if not root.exists():
            return {"files": []}
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append(str(path.relative_to(self.memory_root)))
        return {"files": files}

    def read_memory(self, params: JsonDict) -> JsonDict:
        path = self._safe_path(str(params["path"]))
        with open(path, "r", encoding="utf-8") as handle:
            return {"path": str(path.relative_to(self.memory_root)), "content": handle.read()}

    def write_memory(self, params: JsonDict) -> JsonDict:
        path = self._safe_path(str(params["path"]))
        content = str(params.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return {"status": "ok", "path": str(path.relative_to(self.memory_root))}

    def append_memory(self, params: JsonDict) -> JsonDict:
        path = self._safe_path(str(params["path"]))
        content = str(params.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(content)
        return {"status": "ok", "path": str(path.relative_to(self.memory_root))}

    def _safe_path(self, relative: str) -> Path:
        path = (self.memory_root / relative).resolve()
        try:
            path.relative_to(self.memory_root)
        except ValueError as exc:
            raise ValueError("memory path escapes memory_root") from exc
        return path


def _response(request_id: Any, result: Any = None, error: str | None = None) -> JsonDict:
    response: JsonDict = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        response["result"] = result
    else:
        response["error"] = {"code": -32000, "message": error}
    return response


def serve(config_path: str) -> int:
    server = PiRpcServer(load_pi_config(config_path))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = str(request.get("method") or "")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            result = server.dispatch(method, params)
            print(json.dumps(_response(request_id, result), ensure_ascii=False), flush=True)
        except Exception as exc:
            request_id = None
            try:
                request_id = json.loads(line).get("id")
            except Exception:
                pass
            print(json.dumps(_response(request_id, error=str(exc))), flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pi Agent JSON-RPC stdio server.")
    parser.add_argument(
        "--config",
        default=str(HARNESS_ROOT / "pi_agent" / "config" / "pi_agent.json"),
        help="Pi agent RPC config JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
