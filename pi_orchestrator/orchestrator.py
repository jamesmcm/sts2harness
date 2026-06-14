from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HARNESS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = HARNESS_ROOT / "pi_orchestrator" / "config" / "orchestrator.local.json"

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: float = 300.0


@dataclass(frozen=True)
class OrchestratorConfig:
    rpc_server_command: list[str]
    model: ModelConfig
    max_steps: int = 200
    stop_on_game_over: bool = True
    decision_log: str = str(HARNESS_ROOT / "pi_orchestrator" / "runs.jsonl")
    prompt_template: str = str(
        HARNESS_ROOT / "pi_orchestrator" / "prompts" / "decision_prompt.md"
    )


def _load_json(path: str | Path) -> JsonDict:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_config(path: str | Path) -> OrchestratorConfig:
    raw = _load_json(path)
    model_raw = raw.get("model")
    if not isinstance(model_raw, dict):
        raise ValueError("config.model is required")
    command = raw.get("rpc_server_command")
    if not isinstance(command, list) or not command:
        raise ValueError("config.rpc_server_command must be a non-empty list")
    return OrchestratorConfig(
        rpc_server_command=[str(part) for part in command],
        model=ModelConfig(
            provider=str(model_raw.get("provider") or "openai_responses"),
            model=str(model_raw["model"]),
            api_key_env=_optional_str(model_raw.get("api_key_env")),
            base_url=_optional_str(model_raw.get("base_url")),
            timeout=float(model_raw.get("timeout", 300.0)),
        ),
        max_steps=int(raw.get("max_steps", 200)),
        stop_on_game_over=bool(raw.get("stop_on_game_over", True)),
        decision_log=str(raw.get("decision_log") or HARNESS_ROOT / "pi_orchestrator" / "runs.jsonl"),
        prompt_template=str(
            raw.get("prompt_template")
            or HARNESS_ROOT / "pi_orchestrator" / "prompts" / "decision_prompt.md"
        ),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class JsonRpcClient:
    def __init__(self, command: list[str]) -> None:
        self.process = subprocess.Popen(
            command,
            cwd=str(HARNESS_ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def call(self, method: str, params: JsonDict | None = None) -> Any:
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("RPC process pipes are closed")
        request_id = self.next_id
        self.next_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            stderr = ""
            if self.process.stderr is not None:
                stderr = self.process.stderr.read()
            raise RuntimeError(f"RPC process exited without response: {stderr}")
        response = json.loads(line)
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                raise RuntimeError(str(error.get("message") or error))
            raise RuntimeError(str(error))
        return response.get("result")


class ModelProvider:
    def complete_json(self, prompt: str) -> JsonDict:
        raise NotImplementedError


class HttpJsonProvider(ModelProvider):
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.schema = _load_json(
            HARNESS_ROOT / "pi_orchestrator" / "schemas" / "decision.schema.json"
        )
        env_name = config.api_key_env or "OPENAI_API_KEY"
        api_key = os.environ.get(env_name)
        if not api_key:
            raise RuntimeError(f"Missing API key environment variable: {env_name}")
        self.api_key = api_key

    def _post_json(self, url: str, payload: JsonDict) -> JsonDict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout
            ) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model API HTTP {exc.code}: {body}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("model API returned a non-object JSON response")
        return value


class OpenAIResponsesProvider(HttpJsonProvider):
    def complete_json(self, prompt: str) -> JsonDict:
        base_url = self.config.base_url or "https://api.openai.com/v1"
        payload: JsonDict = {
            "model": self.config.model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sts2_decision",
                    "strict": True,
                    "schema": self.schema,
                }
            },
        }
        response = self._post_json(f"{base_url.rstrip('/')}/responses", payload)
        text = response.get("output_text")
        if isinstance(text, str) and text.strip():
            return _parse_json_text(text)
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return _parse_json_text(part["text"])
        raise RuntimeError("OpenAI Responses API returned no text output")


class OpenAICompatibleChatProvider(HttpJsonProvider):
    def complete_json(self, prompt: str) -> JsonDict:
        if not self.config.base_url:
            raise RuntimeError("openai_compatible_chat requires model.base_url")
        payload: JsonDict = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "sts2_decision",
                    "strict": True,
                    "schema": self.schema,
                },
            },
        }
        response = self._post_json(
            f"{self.config.base_url.rstrip('/')}/chat/completions", payload
        )
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return _parse_json_text(message["content"])
        raise RuntimeError("OpenAI-compatible API returned no message content")


class OpenRouterProvider(OpenAICompatibleChatProvider):
    def __init__(self, config: ModelConfig) -> None:
        config = ModelConfig(
            provider=config.provider,
            model=config.model,
            api_key_env=config.api_key_env or "OPENROUTER_API_KEY",
            base_url=config.base_url or "https://openrouter.ai/api/v1",
            timeout=config.timeout,
        )
        super().__init__(config)


def make_provider(config: ModelConfig) -> ModelProvider:
    if config.provider == "openai_responses":
        return OpenAIResponsesProvider(config)
    if config.provider == "openai_compatible_chat":
        return OpenAICompatibleChatProvider(config)
    if config.provider == "openrouter":
        return OpenRouterProvider(config)
    raise ValueError(
        "model.provider must be openai_responses, openai_compatible_chat, or openrouter"
    )


def _parse_json_text(text: str) -> JsonDict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        matches = re.findall(r"\{(?:.|\n)*\}", text)
        if not matches:
            raise
        value = json.loads(matches[-1])
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def build_prompt(template_path: str, snapshot: JsonDict, memory: JsonDict) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    return template.replace(
        "{{SNAPSHOT_JSON}}", json.dumps(snapshot, indent=2, sort_keys=True)
    ).replace("{{MEMORY_JSON}}", json.dumps(memory, indent=2, sort_keys=True))


def read_memory_bundle(rpc: JsonRpcClient) -> JsonDict:
    bundle: JsonDict = {}
    for path in ("STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md"):
        try:
            bundle[path] = rpc.call("read_memory", {"path": path})["content"]
        except Exception as exc:
            bundle[path] = f"[unavailable: {exc}]"
    return bundle


def apply_memory_updates(rpc: JsonRpcClient, decision: JsonDict) -> None:
    updates = decision.get("memory_updates")
    if not isinstance(updates, list):
        return
    for update in updates:
        if not isinstance(update, dict):
            continue
        path = update.get("path")
        content = update.get("content")
        if path is None or content is None:
            continue
        mode = str(update.get("mode") or "append")
        method = "write_memory" if mode == "write" else "append_memory"
        rpc.call(method, {"path": str(path), "content": str(content)})


def validate_action(decision: JsonDict, actions: list[JsonDict]) -> str:
    action_ref = decision.get("action_ref")
    if action_ref is None:
        raise ValueError("decision.action_ref is required")
    text = str(action_ref)
    if text.isdigit():
        index = int(text)
        if 0 <= index < len(actions):
            return text
    legal_ids = {str(action.get("id")) for action in actions}
    if text in legal_ids:
        return text
    raise ValueError(f"model chose illegal action_ref {text!r}")


def append_decision_log(path: str, record: JsonDict) -> None:
    log_path = Path(path).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def run(config: OrchestratorConfig) -> int:
    provider = make_provider(config.model)
    rpc = JsonRpcClient(config.rpc_server_command)
    try:
        rpc.call("ping", {})
        for step in range(config.max_steps):
            snapshot = rpc.call("snapshot", {})
            state = snapshot.get("state") if isinstance(snapshot, dict) else {}
            actions = snapshot.get("actions") if isinstance(snapshot, dict) else []
            if not isinstance(state, dict) or not isinstance(actions, list):
                raise RuntimeError("snapshot returned malformed state/actions")
            if config.stop_on_game_over and state.get("state_type") == "game_over":
                append_decision_log(
                    config.decision_log,
                    {
                        "timestamp": time.time(),
                        "step": step,
                        "event": "game_over",
                        "snapshot": snapshot,
                    },
                )
                return 0
            if not actions:
                raise RuntimeError("no legal actions available")
            memory = read_memory_bundle(rpc)
            prompt = build_prompt(config.prompt_template, snapshot, memory)
            decision = provider.complete_json(prompt)
            action_ref = validate_action(decision, actions)
            apply_memory_updates(rpc, decision)
            result = rpc.call("act", {"action": action_ref})
            append_decision_log(
                config.decision_log,
                {
                    "timestamp": time.time(),
                    "step": step,
                    "action_ref": action_ref,
                    "decision": decision,
                    "result_summary": _result_summary(result),
                },
            )
        return 0
    finally:
        rpc.close()


def _result_summary(result: Any) -> JsonDict:
    if not isinstance(result, dict):
        return {"result": result}
    state = result.get("state")
    if not isinstance(state, dict):
        state = {}
    return {
        "action": result.get("action"),
        "status": result.get("result", {}).get("status")
        if isinstance(result.get("result"), dict)
        else None,
        "state_type": state.get("state_type"),
        "menu_screen": state.get("menu_screen"),
        "actions_count": len(result.get("actions") or []),
        "progress_update": result.get("progress_update"),
        "run_setup_verification": result.get("run_setup_verification"),
        "auto_actions_count": len(result.get("auto_actions") or []),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Pi LLM orchestrator.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(load_config(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
