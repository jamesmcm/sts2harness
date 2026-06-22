from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HARNESS_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = HARNESS_ROOT / "pi_orchestrator" / "config" / "orchestrator.local.json"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_GO_DEFAULT_MODEL = "deepseek-v4-flash"
OPENCODE_GO_API_KEY_ENV = "OPENCODE_API_KEY"
OPENCODE_GO_AUTH_PROVIDERS = ("opencode-go", "oc-sdk-go")
WRITABLE_MEMORY_FILES = frozenset(
    {"STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md", "HARNESS_BUGS.md"}
)
HTTP_USER_AGENT = "sts2harness/0.1"
DEFAULT_MODEL_RETRY_BACKOFFS = (30.0, 60.0, 120.0, 240.0, 300.0)
TRANSIENT_MODEL_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MODEL_DECISION_PARSE_RETRIES = 2
MODEL_DECISION_PARSE_RETRY_DELAY = 2.0
MEMORY_FILE_TEMPLATES = {
    "CURRENT_RUN.md": (
        "# Current Run\n\n"
        "Rewrite this file as the active run sheet: seed, ascension, floor, "
        "HP/gold, deck list and upgrades, relic synergies, potions, key recent "
        "combats, reward picks and skips, route/boss plan, current risks, and "
        "tactical priorities.\n"
    ),
    "BATTLE_LOG.md": (
        "# Battle Log\n\n"
        "Temporary scratchpad for the current battle only. Rewrite during combat, "
        "then fold useful lessons into `CURRENT_RUN.md` and clear this file after "
        "combat.\n"
    ),
}

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key_env: str | None = None
    base_url: str | None = None
    response_format: str | None = None
    timeout: float = 300.0
    retry_backoffs: tuple[float, ...] = DEFAULT_MODEL_RETRY_BACKOFFS


@dataclass(frozen=True)
class OrchestratorConfig:
    rpc_server_command: list[str]
    model: ModelConfig
    max_steps: int | None = None
    stop_on_game_over: bool = True
    empty_action_max_wait: float = 20.0
    empty_action_poll_interval: float = 1.0
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
            model=_model_name(model_raw),
            api_key_env=_optional_str(model_raw.get("api_key_env")),
            base_url=_optional_str(model_raw.get("base_url")),
            response_format=_optional_str(model_raw.get("response_format")),
            timeout=float(model_raw.get("timeout", 300.0)),
            retry_backoffs=_retry_backoffs(model_raw.get("retry_backoffs")),
        ),
        max_steps=_optional_positive_int(raw.get("max_steps")),
        stop_on_game_over=bool(raw.get("stop_on_game_over", True)),
        empty_action_max_wait=float(raw.get("empty_action_max_wait", 20.0)),
        empty_action_poll_interval=float(raw.get("empty_action_poll_interval", 1.0)),
        decision_log=str(
            raw.get("decision_log") or HARNESS_ROOT / "pi_orchestrator" / "runs.jsonl"
        ),
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


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    result = int(value)
    return result if result > 0 else None


def _retry_backoffs(value: Any) -> tuple[float, ...]:
    if value is None:
        return DEFAULT_MODEL_RETRY_BACKOFFS
    if not isinstance(value, list):
        raise ValueError("config.model.retry_backoffs must be a list of seconds")
    backoffs: list[float] = []
    for item in value:
        seconds = float(item)
        if seconds < 0:
            raise ValueError("config.model.retry_backoffs cannot contain negatives")
        backoffs.append(seconds)
    return tuple(backoffs)


def _model_name(model_raw: JsonDict) -> str:
    value = _optional_str(model_raw.get("model"))
    if value is not None:
        return value
    if str(model_raw.get("provider") or "") == "opencode_go":
        return OPENCODE_GO_DEFAULT_MODEL
    raise ValueError("config.model.model is required")


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
    def complete_json(self, prompt: str) -> "ModelCompletion":
        raise NotImplementedError


@dataclass(frozen=True)
class ModelCompletion:
    decision: JsonDict
    raw_response: JsonDict
    response_text: str
    request_payload: JsonDict
    provider_name: str | None = None
    request_id: str | None = None
    response_id: str | None = None
    generation_id: str | None = None
    upstream_id: str | None = None
    total_cost: float | None = None
    prompt_cost: float | None = None
    completion_cost: float | None = None
    native_tokens_prompt: int | None = None
    native_tokens_completion: int | None = None
    generation_stats: JsonDict | None = None
    generation_content: JsonDict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ModelResponseFormatError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_response: JsonDict,
        response_text: str,
        request_payload: JsonDict,
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.response_text = response_text
        self.request_payload = request_payload


class HttpJsonProvider(ModelProvider):
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.schema = _load_json(
            HARNESS_ROOT / "pi_orchestrator" / "schemas" / "decision.schema.json"
        )
        env_name = config.api_key_env or "OPENAI_API_KEY"
        api_key = self._resolve_api_key(env_name)
        if not api_key:
            raise RuntimeError(self._missing_api_key_message(env_name))
        self.api_key = api_key

    def _resolve_api_key(self, env_name: str) -> str | None:
        return os.environ.get(env_name)

    def _missing_api_key_message(self, env_name: str) -> str:
        return f"Missing API key environment variable: {env_name}"

    def _post_json(self, url: str, payload: JsonDict) -> JsonDict:
        return self._request_json("POST", url, payload)

    def _get_json(self, url: str) -> JsonDict:
        return self._request_json("GET", url, None)

    def _request_json(
        self, method: str, url: str, payload: JsonDict | None
    ) -> JsonDict:
        attempts = len(self.config.retry_backoffs) + 1
        for attempt in range(attempts):
            request = self._json_request(method, url, payload)
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout
                ) as response:
                    value = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if not self._should_retry_http(exc.code, attempt):
                    raise RuntimeError(f"model API HTTP {exc.code}: {body}") from exc
                self._sleep_before_retry(method, attempt, f"HTTP {exc.code}")
                continue
            except (TimeoutError, urllib.error.URLError) as exc:
                if not self._should_retry_network(attempt):
                    raise RuntimeError(f"model API request failed: {exc}") from exc
                self._sleep_before_retry(method, attempt, exc.__class__.__name__)
                continue
            if not isinstance(value, dict):
                raise RuntimeError("model API returned a non-object JSON response")
            return value
        raise RuntimeError("model API request failed after retries")

    def _json_request(
        self, method: str, url: str, payload: JsonDict | None
    ) -> urllib.request.Request:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": HTTP_USER_AGENT,
            },
            method=method,
        )
        return request

    def _should_retry_http(self, code: int, attempt: int) -> bool:
        return (
            code in TRANSIENT_MODEL_HTTP_STATUS_CODES
            and attempt < len(self.config.retry_backoffs)
        )

    def _should_retry_network(self, attempt: int) -> bool:
        return attempt < len(self.config.retry_backoffs)

    def _sleep_before_retry(self, method: str, attempt: int, reason: str) -> None:
        delay = self.config.retry_backoffs[attempt]
        print(
            f"model API {method} failed with {reason}; "
            f"retrying in {delay:g}s "
            f"(attempt {attempt + 2}/{len(self.config.retry_backoffs) + 1})",
            file=sys.stderr,
            flush=True,
        )
        if delay > 0:
            time.sleep(delay)


class OpenAIResponsesProvider(HttpJsonProvider):
    def complete_json(self, prompt: str) -> ModelCompletion:
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
            return _completion_from_text(response, text, payload)
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
                        text = part["text"]
                        return _completion_from_text(response, text, payload)
        raise RuntimeError("OpenAI Responses API returned no text output")


class OpenAICompatibleChatProvider(HttpJsonProvider):
    def complete_json(self, prompt: str) -> ModelCompletion:
        if not self.config.base_url:
            raise RuntimeError("openai_compatible_chat requires model.base_url")
        modes = self._response_format_modes()
        last_error: RuntimeError | None = None
        for mode in modes:
            payload = self._chat_payload(prompt, mode)
            try:
                response = self._post_json(
                    f"{self.config.base_url.rstrip('/')}/chat/completions", payload
                )
            except RuntimeError as exc:
                if mode == "json_schema" and _response_format_unavailable(exc):
                    last_error = exc
                    continue
                raise
            return self._completion_from_chat_response(response, payload)
        if last_error is not None:
            raise last_error
        raise RuntimeError("no response_format modes configured")

    def _response_format_modes(self) -> list[str]:
        mode = self.config.response_format or "json_schema"
        if mode == "json_schema":
            return ["json_schema", "json_object", "none"]
        return [mode]

    def _chat_payload(self, prompt: str, response_format: str) -> JsonDict:
        payload: JsonDict = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }
        if response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sts2_decision",
                    "strict": True,
                    "schema": self.schema,
                },
            }
        elif response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif response_format != "none":
            raise ValueError(
                "model.response_format must be json_schema, json_object, or none"
            )
        return payload

    def _completion_from_chat_response(
        self, response: JsonDict, payload: JsonDict
    ) -> ModelCompletion:
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = (
                choices[0].get("message") if isinstance(choices[0], dict) else None
            )
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                text = message["content"]
                return _completion_from_text(response, text, payload)
        raise RuntimeError("OpenAI-compatible API returned no message content")


def _response_format_unavailable(exc: RuntimeError) -> bool:
    text = str(exc).lower()
    return "response_format" in text and (
        "unavailable" in text or "unsupported" in text or "not support" in text
    )


class OpenRouterProvider(OpenAICompatibleChatProvider):
    def __init__(self, config: ModelConfig) -> None:
        config = ModelConfig(
            provider=config.provider,
            model=config.model,
            api_key_env=config.api_key_env or "OPENROUTER_API_KEY",
            base_url=config.base_url or "https://openrouter.ai/api/v1",
            timeout=config.timeout,
            retry_backoffs=config.retry_backoffs,
        )
        super().__init__(config)

    def complete_json(self, prompt: str) -> ModelCompletion:
        completion = super().complete_json(prompt)
        generation_id = completion.generation_id or completion.response_id
        generation_stats = completion.generation_stats
        generation_content = completion.generation_content
        if generation_id:
            generation_stats = self._get_generation_json("generation", generation_id)
            generation_content = self._get_generation_json(
                "generation/content", generation_id
            )
        return _enrich_openrouter_completion(
            completion,
            generation_stats=generation_stats,
            generation_content=generation_content,
        )

    def _get_generation_json(self, endpoint: str, generation_id: str) -> JsonDict | None:
        base_url = self.config.base_url or "https://openrouter.ai/api/v1"
        query = urllib.parse.urlencode({"id": generation_id})
        url = f"{base_url.rstrip('/')}/{endpoint}?{query}"
        for attempt in range(3):
            if attempt:
                time.sleep(0.75 * attempt)
            try:
                return self._get_json(url)
            except RuntimeError as exc:
                if any(f"HTTP {code}" in str(exc) for code in (404, 429, 500, 502)):
                    continue
                return {"error": str(exc)}
            except (TimeoutError, urllib.error.URLError):
                continue
        return None


class OpenCodeGoProvider(OpenAICompatibleChatProvider):
    def __init__(self, config: ModelConfig) -> None:
        config = ModelConfig(
            provider=config.provider,
            model=config.model or OPENCODE_GO_DEFAULT_MODEL,
            api_key_env=config.api_key_env or OPENCODE_GO_API_KEY_ENV,
            base_url=config.base_url or OPENCODE_GO_BASE_URL,
            response_format=config.response_format or "json_object",
            timeout=config.timeout,
            retry_backoffs=config.retry_backoffs,
        )
        super().__init__(config)

    def _resolve_api_key(self, env_name: str) -> str | None:
        key = os.environ.get(env_name)
        if key:
            return key
        if env_name != OPENCODE_GO_API_KEY_ENV:
            key = os.environ.get(OPENCODE_GO_API_KEY_ENV)
            if key:
                return key
        return _read_opencode_go_auth_key()

    def _missing_api_key_message(self, env_name: str) -> str:
        fallback_env = (
            ""
            if env_name == OPENCODE_GO_API_KEY_ENV
            else f" or {OPENCODE_GO_API_KEY_ENV}"
        )
        return (
            f"Missing OpenCode Go API key. Set {env_name}{fallback_env} "
            "or save a key in ~/.local/share/opencode/auth.json "
            "or ~/.pi/agent/auth.json."
        )


def _read_opencode_go_auth_key() -> str | None:
    for path in (
        Path.home() / ".local" / "share" / "opencode" / "auth.json",
        Path.home() / ".pi" / "agent" / "auth.json",
    ):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                auth = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(auth, dict):
            continue
        for provider in OPENCODE_GO_AUTH_PROVIDERS:
            provider_auth = auth.get(provider)
            if not isinstance(provider_auth, dict):
                continue
            key = _optional_str(provider_auth.get("key"))
            if key is not None:
                return key
    return None


def make_provider(config: ModelConfig) -> ModelProvider:
    if config.provider == "openai_responses":
        return OpenAIResponsesProvider(config)
    if config.provider == "openai_compatible_chat":
        return OpenAICompatibleChatProvider(config)
    if config.provider == "opencode_go":
        return OpenCodeGoProvider(config)
    if config.provider == "openrouter":
        return OpenRouterProvider(config)
    raise ValueError(
        "model.provider must be openai_responses, openai_compatible_chat, "
        "opencode_go, or openrouter"
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


def _completion_from_text(
    response: JsonDict,
    response_text: str,
    request_payload: JsonDict,
) -> ModelCompletion:
    try:
        decision = _parse_json_text(response_text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ModelResponseFormatError(
            f"model returned invalid decision JSON: {exc}",
            raw_response=response,
            response_text=response_text,
            request_payload=request_payload,
        ) from exc
    return _completion_from_response(decision, response, response_text, request_payload)


def _completion_from_response(
    decision: JsonDict,
    response: JsonDict,
    response_text: str,
    request_payload: JsonDict,
) -> ModelCompletion:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    response_id = _optional_str(response.get("id"))
    return ModelCompletion(
        decision=decision,
        raw_response=response,
        response_text=response_text,
        request_payload=request_payload,
        response_id=response_id,
        generation_id=response_id,
        input_tokens=_usage_int(
            usage, "input_tokens", "prompt_tokens", "total_input_tokens"
        ),
        output_tokens=_usage_int(
            usage, "output_tokens", "completion_tokens", "total_output_tokens"
        ),
        total_tokens=_usage_int(usage, "total_tokens"),
    )


def _enrich_openrouter_completion(
    completion: ModelCompletion,
    *,
    generation_stats: JsonDict | None,
    generation_content: JsonDict | None,
) -> ModelCompletion:
    data = _generation_data(generation_stats)
    content_data = _generation_data(generation_content)
    prompt_text = None
    response_text = completion.response_text
    if content_data:
        input_data = content_data.get("input")
        if isinstance(input_data, dict):
            prompt_text = _optional_str(input_data.get("prompt"))
        output_data = content_data.get("output")
        if isinstance(output_data, dict):
            response_text = _optional_str(output_data.get("completion")) or response_text

    total_cost = _float_from_data(data, "total_cost", "usage")
    native_prompt = _int_from_data(data, "native_tokens_prompt", "tokens_prompt")
    native_completion = _int_from_data(
        data, "native_tokens_completion", "tokens_completion"
    )
    prompt_cost, completion_cost = _split_cost(
        total_cost, native_prompt, native_completion
    )
    return ModelCompletion(
        decision=completion.decision,
        raw_response=completion.raw_response,
        response_text=response_text,
        request_payload=completion.request_payload,
        provider_name=_optional_str(data.get("provider_name")),
        request_id=_optional_str(data.get("request_id")),
        response_id=completion.response_id,
        generation_id=_optional_str(data.get("id")) or completion.generation_id,
        upstream_id=_optional_str(data.get("upstream_id")),
        total_cost=total_cost,
        prompt_cost=prompt_cost,
        completion_cost=completion_cost,
        native_tokens_prompt=native_prompt,
        native_tokens_completion=native_completion,
        generation_stats=generation_stats,
        generation_content=generation_content,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        total_tokens=completion.total_tokens,
    )


def _generation_data(value: JsonDict | None) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _int_from_data(data: JsonDict, *keys: str) -> int | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _float_from_data(data: JsonDict, *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _split_cost(
    total_cost: float | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> tuple[float | None, float | None]:
    if total_cost is None or prompt_tokens is None or completion_tokens is None:
        return None, None
    token_total = prompt_tokens + completion_tokens
    if token_total <= 0:
        return None, None
    prompt_cost = total_cost * (prompt_tokens / token_total)
    return prompt_cost, total_cost - prompt_cost


def _usage_int(usage: JsonDict, *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _tokens_per_second(tokens: int | None, elapsed_seconds: float | None) -> float | None:
    if tokens is None or elapsed_seconds is None or elapsed_seconds <= 0:
        return None
    return tokens / elapsed_seconds


def _completion_total_tokens(completion: ModelCompletion) -> int | None:
    if completion.total_tokens is not None:
        return completion.total_tokens
    if completion.input_tokens is None or completion.output_tokens is None:
        return None
    return completion.input_tokens + completion.output_tokens


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_prompt(template_path: str, snapshot: JsonDict, memory: JsonDict) -> str:
    template = Path(template_path).read_text(encoding="utf-8")
    prompt = template.replace(
        "{{SNAPSHOT_JSON}}", json.dumps(snapshot, indent=2, sort_keys=True)
    ).replace("{{MEMORY_JSON}}", json.dumps(memory, indent=2, sort_keys=True))
    lifecycle_requirement = build_lifecycle_requirement(snapshot, memory)
    if lifecycle_requirement:
        prompt += f"\n\nLifecycle requirement:\n{lifecycle_requirement}\n"
    return prompt


def read_memory_bundle(rpc: JsonRpcClient) -> JsonDict:
    bundle: JsonDict = {}
    for path in ("STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md", "HARNESS_BUGS.md"):
        try:
            bundle[path] = rpc.call("read_memory", {"path": path})["content"]
        except Exception as exc:
            bundle[path] = f"[unavailable: {exc}]"
    return bundle


def apply_memory_updates(rpc: JsonRpcClient, decision: JsonDict) -> set[str]:
    updates = decision.get("memory_updates")
    if not isinstance(updates, list):
        return set()
    written: set[str] = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        path = update.get("path")
        content = update.get("content")
        if path is None or content is None:
            continue
        if str(path) not in WRITABLE_MEMORY_FILES:
            continue
        if str(update.get("mode") or "") != "write":
            continue
        path_text = str(path)
        current = rpc.call("read_memory", {"path": path_text})
        current_content = (
            current.get("content") if isinstance(current, dict) else None
        )
        if not isinstance(current_content, str):
            raise RuntimeError(f"could not read current {path_text} before writing")
        rpc.call("write_memory", {"path": path_text, "content": str(content)})
        written.add(path_text)
    return written


def memory_update_write_paths(decision: JsonDict) -> set[str]:
    updates = decision.get("memory_updates")
    if not isinstance(updates, list):
        return set()
    paths: set[str] = set()
    for update in updates:
        if not isinstance(update, dict):
            continue
        path = str(update.get("path") or "")
        if path in WRITABLE_MEMORY_FILES and str(update.get("mode") or "") == "write":
            paths.add(path)
    return paths


def memory_update_write_contents(decision: JsonDict) -> dict[str, str]:
    updates = decision.get("memory_updates")
    if not isinstance(updates, list):
        return {}
    contents: dict[str, str] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        path = str(update.get("path") or "")
        if path not in WRITABLE_MEMORY_FILES:
            continue
        if str(update.get("mode") or "") != "write":
            continue
        contents[path] = str(update.get("content") or "")
    return contents


def validate_memory_lifecycle(
    decision: JsonDict, snapshot: JsonDict, memory: JsonDict
) -> str | None:
    required = required_lifecycle_memory_write(snapshot, memory)
    if required is None:
        return None
    path, reason = required
    if path in memory_update_write_paths(decision):
        return None
    return f"{reason}; rewrite {path} before advancing"


def validate_memory_quality(
    decision: JsonDict, snapshot: JsonDict, memory: JsonDict
) -> str | None:
    del snapshot
    contents = memory_update_write_contents(decision)
    for path in ("CURRENT_RUN.md", "STRATEGY.md"):
        if path not in contents:
            continue
        old_content = memory.get(path)
        if not isinstance(old_content, str) or old_content.startswith("[unavailable:"):
            return f"{path} must be read successfully before it can be rewritten"
    return None


def required_lifecycle_memory_write(
    snapshot: JsonDict, memory: JsonDict
) -> tuple[str, str] | None:
    if _snapshot_has_failed_completed_run(snapshot):
        return (
            "STRATEGY.md",
            "completed failed run requires a durable strategy revision",
        )
    if _snapshot_has_finished_battle(snapshot) and _memory_file_has_active_notes(
        memory.get("BATTLE_LOG.md"), "BATTLE_LOG.md"
    ):
        return (
            "CURRENT_RUN.md",
            "finished battle requires folding BATTLE_LOG.md into CURRENT_RUN.md",
        )
    return None


def build_lifecycle_requirement(snapshot: JsonDict, memory: JsonDict) -> str:
    required = required_lifecycle_memory_write(snapshot, memory)
    if required is None:
        return ""
    path, reason = required
    if path == "STRATEGY.md":
        return (
            f"- {reason}. Before choosing the restart/advance action, rewrite "
            "`STRATEGY.md` with comprehensive lessons from the completed run: "
            "why it died, what card/relic/pathing/tactical choices mattered, and "
            "what to do differently in future runs. `STRATEGY.md` is your only "
            "persistent cross-run playbook, so do not make a sparse summary; "
            "read the full `STRATEGY.md`, `CURRENT_RUN.md`, and `BATTLE_LOG.md` "
            "contents already provided in this same prompt's `Memory` section and "
            "preserve all useful learning needed to play better next time. This "
            "refinement must stand on its own because the harness will then clear "
            "`CURRENT_RUN.md` and `BATTLE_LOG.md` before the new run continues."
        )
    return (
        f"- {reason}. Before choosing the next reward/map/event action, rewrite "
        "`CURRENT_RUN.md` with the complete battle outcome, HP/potion changes, "
        "deck/relic state, tactical lessons, and next priorities. `CURRENT_RUN.md` "
        "is your only persistent context for this run, so do not make a sparse "
        "summary; read the full `CURRENT_RUN.md` and `BATTLE_LOG.md` contents "
        "already provided in this same prompt's `Memory` section and preserve "
        "enough detail to resume good play later. This refinement must preserve "
        "every useful lesson from `BATTLE_LOG.md`; it must stand on its own "
        "because the harness will then clear `BATTLE_LOG.md`."
    )


def enforce_memory_lifecycle(
    rpc: JsonRpcClient, snapshot: JsonDict, written_paths: set[str]
) -> list[JsonDict]:
    records: list[JsonDict] = []
    cleared_battle_log = False

    if "CURRENT_RUN.md" in written_paths and _snapshot_has_finished_battle(snapshot):
        rpc.call(
            "write_memory",
            {
                "path": "BATTLE_LOG.md",
                "content": MEMORY_FILE_TEMPLATES["BATTLE_LOG.md"],
            },
        )
        records.append(
            {
                "path": "BATTLE_LOG.md",
                "reason": "current_run_rewritten_after_battle",
            }
        )
        cleared_battle_log = True

    if _snapshot_has_failed_completed_run(snapshot) and "STRATEGY.md" in written_paths:
        if "CURRENT_RUN.md" not in written_paths:
            rpc.call(
                "write_memory",
                {
                    "path": "CURRENT_RUN.md",
                    "content": MEMORY_FILE_TEMPLATES["CURRENT_RUN.md"],
                },
            )
            records.append(
                {
                    "path": "CURRENT_RUN.md",
                    "reason": "strategy_rewritten_after_death",
                }
            )
        if not cleared_battle_log:
            rpc.call(
                "write_memory",
                {
                    "path": "BATTLE_LOG.md",
                    "content": MEMORY_FILE_TEMPLATES["BATTLE_LOG.md"],
                },
            )
            records.append(
                {
                    "path": "BATTLE_LOG.md",
                    "reason": "strategy_rewritten_after_death",
                }
            )

    return records


def _snapshot_has_finished_battle(snapshot: JsonDict) -> bool:
    state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    battle = state.get("battle") if isinstance(state, dict) else None
    if not isinstance(battle, dict):
        return False
    enemies = battle.get("enemies")
    if not isinstance(enemies, list):
        return False
    return all(_enemy_is_dead(enemy) for enemy in enemies)


def _enemy_is_dead(enemy: Any) -> bool:
    if not isinstance(enemy, dict):
        return False
    for key in ("is_dead", "dead", "isDying"):
        value = enemy.get(key)
        if isinstance(value, bool):
            return value
    for key in ("hp", "current_hp", "currentHealth", "health"):
        value = enemy.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value <= 0
    return False


def _snapshot_has_failed_completed_run(value: Any) -> bool:
    if isinstance(value, dict):
        progress = value.get("progress_update")
        if isinstance(progress, dict) and progress.get("last_completed_victory") is False:
            return True
        return any(_snapshot_has_failed_completed_run(child) for child in value.values())
    if isinstance(value, list):
        return any(_snapshot_has_failed_completed_run(child) for child in value)
    return False


def _memory_file_has_active_notes(value: Any, path: str) -> bool:
    if not isinstance(value, str):
        return False
    text = _normalize_memory_text(value)
    if not text:
        return False
    template = MEMORY_FILE_TEMPLATES.get(path)
    if template is not None and text == _normalize_memory_text(template):
        return False
    if path == "BATTLE_LOG.md":
        seed_template = (HARNESS_ROOT / "pi_agent" / "memory" / "BATTLE_LOG.md")
        try:
            if text == _normalize_memory_text(seed_template.read_text(encoding="utf-8")):
                return False
        except OSError:
            pass
    return True


def _normalize_memory_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def count_memory_updates(decision: JsonDict) -> int:
    updates = decision.get("memory_updates")
    if not isinstance(updates, list):
        return 0
    return sum(
        1
        for update in updates
        if (
            isinstance(update, dict)
            and str(update.get("path")) in WRITABLE_MEMORY_FILES
            and str(update.get("mode") or "") == "write"
        )
    )


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


def next_decision_step(path: str) -> int:
    log_path = Path(path).expanduser().resolve()
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    max_step = -1
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        step = record.get("step") if isinstance(record, dict) else None
        if isinstance(step, int) and not isinstance(step, bool):
            max_step = max(max_step, step)
    return max_step + 1


def snapshot_with_actions(
    rpc: JsonRpcClient, config: OrchestratorConfig, step: int
) -> tuple[JsonDict, JsonDict, list[JsonDict]]:
    start = time.monotonic()
    attempts = 0
    while True:
        snapshot = rpc.call("snapshot", {})
        state = snapshot.get("state") if isinstance(snapshot, dict) else {}
        actions = snapshot.get("actions") if isinstance(snapshot, dict) else []
        if not isinstance(state, dict) or not isinstance(actions, list):
            raise RuntimeError("snapshot returned malformed state/actions")
        if actions or (
            config.stop_on_game_over and state.get("state_type") == "game_over"
        ):
            if attempts:
                append_decision_log(
                    config.decision_log,
                    {
                        "timestamp": time.time(),
                        "step": step,
                        "event": "empty_actions_resolved",
                        "attempts": attempts,
                        "wait_seconds": time.monotonic() - start,
                        "state_type": state.get("state_type"),
                        "actions_count": len(actions),
                    },
                )
            return snapshot, state, actions

        elapsed = time.monotonic() - start
        if elapsed >= config.empty_action_max_wait:
            append_decision_log(
                config.decision_log,
                {
                    "timestamp": time.time(),
                    "step": step,
                    "event": "empty_actions_timeout",
                    "attempts": attempts,
                    "wait_seconds": elapsed,
                    "state_type": state.get("state_type"),
                    "snapshot": snapshot,
                },
            )
            raise RuntimeError("no legal actions available")

        attempts += 1
        sleep_for = min(
            config.empty_action_poll_interval,
            max(0.0, config.empty_action_max_wait - elapsed),
        )
        if sleep_for > 0:
            time.sleep(sleep_for)


def complete_with_response_retries(
    provider: ModelProvider, config: OrchestratorConfig, prompt: str, step: int
) -> ModelCompletion:
    for attempt in range(MODEL_DECISION_PARSE_RETRIES + 1):
        try:
            return provider.complete_json(prompt)
        except ModelResponseFormatError as exc:
            final_attempt = attempt >= MODEL_DECISION_PARSE_RETRIES
            append_decision_log(
                config.decision_log,
                {
                    "timestamp": time.time(),
                    "step": step,
                    "event": "invalid_model_json",
                    "attempt": attempt + 1,
                    "max_attempts": MODEL_DECISION_PARSE_RETRIES + 1,
                    "error": str(exc),
                    "response_text": exc.response_text,
                    "raw_response": exc.raw_response,
                    "request_payload": exc.request_payload,
                    "prompt_hash": _sha256_text(prompt),
                    "response_hash": _sha256_json(exc.raw_response),
                    "final_attempt": final_attempt,
                },
            )
            if final_attempt:
                return ModelCompletion(
                    decision={
                        "action_ref": "",
                        "rationale": "model returned invalid decision JSON",
                    },
                    raw_response=exc.raw_response,
                    response_text=exc.response_text,
                    request_payload=exc.request_payload,
                )
            time.sleep(MODEL_DECISION_PARSE_RETRY_DELAY)
    raise RuntimeError("unreachable model response retry state")


def run(config: OrchestratorConfig) -> int:
    provider = make_provider(config.model)
    rpc = JsonRpcClient(config.rpc_server_command)
    try:
        rpc.call("ping", {})
        step = next_decision_step(config.decision_log)
        steps_taken = 0
        while config.max_steps is None or steps_taken < config.max_steps:
            snapshot, state, actions = snapshot_with_actions(rpc, config, step)
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
            memory = read_memory_bundle(rpc)
            prompt = build_prompt(config.prompt_template, snapshot, memory)
            model_started = time.monotonic()
            completion = complete_with_response_retries(provider, config, prompt, step)
            model_elapsed_seconds = time.monotonic() - model_started
            decision = completion.decision
            action_ref = str(decision.get("action_ref") or "")
            validation_error = None
            try:
                action_ref = validate_action(decision, actions)
            except ValueError as exc:
                validation_error = str(exc)
            if validation_error is None:
                validation_error = validate_memory_lifecycle(
                    decision, snapshot, memory
                )
            if validation_error is None:
                validation_error = validate_memory_quality(decision, snapshot, memory)
            memory_update_count = 0
            memory_preread_count = 0
            memory_lifecycle: list[JsonDict] = []
            act_count = 0
            if validation_error is None:
                try:
                    written_paths = apply_memory_updates(rpc, decision)
                    memory_preread_count = len(written_paths)
                    memory_lifecycle = enforce_memory_lifecycle(
                        rpc, snapshot, written_paths
                    )
                    memory_update_count = len(written_paths) + len(memory_lifecycle)
                    result = rpc.call("act", {"action": action_ref})
                    act_count = 1
                except RuntimeError as exc:
                    validation_error = str(exc)
                    result = {
                        "status": "rejected",
                        "error": validation_error,
                        "action": action_ref,
                    }
            else:
                result = {
                    "status": "rejected",
                    "error": validation_error,
                    "action": action_ref,
                }
            tool_calls = {
                "snapshot": 1,
                "memory_reads": 4 + memory_preread_count,
                "memory_writes": memory_update_count,
                "act": act_count,
                "total": 5 + memory_preread_count + act_count + memory_update_count,
            }
            telemetry = rpc.call(
                "record_model_telemetry",
                {
                    "prompt_hash": _sha256_text(prompt),
                    "response_hash": _sha256_json(completion.raw_response),
                    "prompt_text": prompt,
                    "response_text": completion.response_text,
                    "raw_response": completion.raw_response,
                    "request_payload": completion.request_payload,
                    "provider_name": completion.provider_name,
                    "request_id": completion.request_id,
                    "response_id": completion.response_id,
                    "generation_id": completion.generation_id,
                    "upstream_id": completion.upstream_id,
                    "total_cost": completion.total_cost,
                    "prompt_cost": completion.prompt_cost,
                    "completion_cost": completion.completion_cost,
                    "native_tokens_prompt": completion.native_tokens_prompt,
                    "native_tokens_completion": completion.native_tokens_completion,
                    "generation_stats": completion.generation_stats,
                    "generation_content": completion.generation_content,
                    "input_tokens": completion.input_tokens,
                    "output_tokens": completion.output_tokens,
                    "model_elapsed_seconds": model_elapsed_seconds,
                    "tool_calls": tool_calls,
                    "model_calls": 1,
                },
            )
            total_tokens = _completion_total_tokens(completion)
            append_decision_log(
                config.decision_log,
                {
                    "timestamp": time.time(),
                    "step": step,
                    "action_ref": action_ref,
                    "decision": decision,
                    "validation_error": validation_error,
                    "usage": {
                        "input_tokens": completion.input_tokens,
                        "output_tokens": completion.output_tokens,
                        "total_tokens": total_tokens,
                    },
                    "model_elapsed_seconds": model_elapsed_seconds,
                    "tokens_per_second": _tokens_per_second(
                        completion.output_tokens, model_elapsed_seconds
                    ),
                    "total_tokens_per_second": _tokens_per_second(
                        total_tokens, model_elapsed_seconds
                    ),
                    "cost": {
                        "total_cost": completion.total_cost,
                        "prompt_cost": completion.prompt_cost,
                        "completion_cost": completion.completion_cost,
                        "native_tokens_prompt": completion.native_tokens_prompt,
                        "native_tokens_completion": completion.native_tokens_completion,
                    },
                    "ids": {
                        "request_id": completion.request_id,
                        "response_id": completion.response_id,
                        "generation_id": completion.generation_id,
                        "upstream_id": completion.upstream_id,
                    },
                    "prompt_hash": _sha256_text(prompt),
                    "response_hash": _sha256_json(completion.raw_response),
                    "tool_calls": tool_calls,
                    "telemetry": telemetry,
                    "result_summary": _result_summary(result),
                    "memory_lifecycle": memory_lifecycle,
                },
            )
            step += 1
            steps_taken += 1
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
        "status": (
            result.get("result", {}).get("status")
            if isinstance(result.get("result"), dict)
            else result.get("status")
        ),
        "error": result.get("error"),
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
