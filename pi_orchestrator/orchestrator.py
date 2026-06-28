from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sqlite3
import ssl
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
LLAMA_SERVER_BASE_URL = "http://127.0.0.1:8080/v1"
LLAMA_SERVER_DEFAULT_MODEL = "qwen35-9b"
WRITABLE_MEMORY_FILES = frozenset(
    {"STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md", "HARNESS_BUGS.md"}
)
HTTP_USER_AGENT = "sts2harness/0.1"
DEFAULT_MODEL_RETRY_BACKOFFS = (30.0, 60.0, 120.0, 240.0, 300.0)
TRANSIENT_MODEL_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
MODEL_DECISION_PARSE_RETRIES = 2
MODEL_DECISION_PARSE_RETRY_DELAY = 2.0
DEFAULT_CONTEXT_COMPACTION_TOKENS = 900_000
ASCENSION_EFFECTS: tuple[tuple[str, str], ...] = (
    ("Swarming Elites", "Elites spawn more often."),
    ("Weary Traveler", "Ancients heal only 80% of missing HP."),
    ("Poverty", "Enemies and treasure chests drop 25% less gold."),
    ("Tight Belt", "Start each run with 1 fewer potion slot."),
    ("Ascender's Bane", "Start each run cursed."),
    ("Inflation", "Merchant card removal is more expensive."),
    ("Scarcity", "Rare and upgraded cards appear less often."),
    ("Tough Enemies", "All enemies have increased max HP."),
    ("Deadly Enemies", "All enemies deal more damage."),
    ("Double Boss", "Fight two bosses at the end of Act 3."),
)
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
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    cache_prompt: bool | None = None
    id_slot: int | None = None
    thinking_mode: str | None = None
    no_thinking_action_threshold: int = 2
    timeout: float = 300.0
    retry_backoffs: tuple[float, ...] = DEFAULT_MODEL_RETRY_BACKOFFS


@dataclass(frozen=True)
class ModelCallOptions:
    thinking: bool | None = None


@dataclass(frozen=True)
class AgentContextConfig:
    enabled: bool = False
    compaction_token_threshold: int = DEFAULT_CONTEXT_COMPACTION_TOKENS
    context_window_tokens: int | None = None
    prompt_cache: bool = True


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
    agent_context: AgentContextConfig = AgentContextConfig()


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
    provider = str(model_raw.get("provider") or "openai_responses")
    model_config = ModelConfig(
        provider=provider,
        model=_model_name(model_raw),
        api_key_env=_optional_str(model_raw.get("api_key_env")),
        base_url=_optional_str(model_raw.get("base_url")),
        response_format=_optional_str(model_raw.get("response_format")),
        max_tokens=_optional_positive_int(model_raw.get("max_tokens")),
        context_window_tokens=_optional_positive_int(
            model_raw.get("context_window_tokens")
        ),
        cache_prompt=_optional_bool(model_raw.get("cache_prompt")),
        id_slot=_optional_nonnegative_int(
            model_raw.get("id_slot", model_raw.get("slot_id"))
        ),
        thinking_mode=_thinking_mode(model_raw.get("thinking_mode"), provider),
        no_thinking_action_threshold=_optional_positive_int(
            model_raw.get("no_thinking_action_threshold")
        )
        or 2,
        timeout=float(model_raw.get("timeout", 300.0)),
        retry_backoffs=_retry_backoffs(model_raw.get("retry_backoffs")),
    )
    return OrchestratorConfig(
        rpc_server_command=[str(part) for part in command],
        model=model_config,
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
        agent_context=_agent_context_config(raw.get("agent_context"), model_config),
    )


def _agent_context_config(value: Any, model: ModelConfig) -> AgentContextConfig:
    default_threshold = _default_context_compaction_tokens(model)
    default_limit = model.context_window_tokens or _default_context_window_tokens(model)
    if value is None:
        return AgentContextConfig(
            compaction_token_threshold=default_threshold,
            context_window_tokens=default_limit,
        )
    if isinstance(value, bool):
        return AgentContextConfig(
            enabled=value,
            compaction_token_threshold=default_threshold,
            context_window_tokens=default_limit,
        )
    if not isinstance(value, dict):
        raise ValueError("config.agent_context must be a boolean or object")
    context_window_tokens = (
        _optional_positive_int(value.get("context_window_tokens"))
        or model.context_window_tokens
        or _default_context_window_tokens(model)
    )
    threshold = int(value.get("compaction_token_threshold") or 0)
    if threshold <= 0:
        threshold = _context_compaction_tokens_for_limit(context_window_tokens)
    if threshold <= 0:
        threshold = default_threshold
    if threshold <= 0:
        raise ValueError("config.agent_context.compaction_token_threshold must be > 0")
    return AgentContextConfig(
        enabled=bool(value.get("enabled", False)),
        compaction_token_threshold=threshold,
        context_window_tokens=context_window_tokens,
        prompt_cache=bool(value.get("prompt_cache", True)),
    )


def _default_context_window_tokens(model: ModelConfig) -> int | None:
    if model.provider == "opencode_go":
        return 1_000_000
    if model.provider == "llama_server":
        return 32_000
    return None


def _default_context_compaction_tokens(model: ModelConfig) -> int:
    limit = model.context_window_tokens or _default_context_window_tokens(model)
    if limit is not None:
        threshold = _context_compaction_tokens_for_limit(limit)
        reserved_output = model.max_tokens
        if reserved_output is None and model.provider == "llama_server":
            reserved_output = 20_000
        if reserved_output is not None:
            threshold = min(threshold, max(1, limit - reserved_output - 4_000))
        return threshold
    return DEFAULT_CONTEXT_COMPACTION_TOKENS


def _context_compaction_tokens_for_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_CONTEXT_COMPACTION_TOKENS
    if limit >= 200_000:
        return max(1, int(limit * 0.8))
    return max(1, int(limit * 0.75))


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


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    result = int(value)
    return result if result >= 0 else None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError("boolean config values must be true or false")


def _thinking_mode(value: Any, provider: str) -> str | None:
    if value is None:
        return "auto" if provider == "llama_server" else None
    mode = str(value).strip().lower()
    if mode in {"auto", "enabled", "disabled"}:
        return mode
    raise ValueError("config.model.thinking_mode must be auto, enabled, or disabled")


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
    provider = str(model_raw.get("provider") or "")
    if provider == "opencode_go":
        return OPENCODE_GO_DEFAULT_MODEL
    if provider == "llama_server":
        return LLAMA_SERVER_DEFAULT_MODEL
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
    def complete_json(
        self, prompt: str, options: ModelCallOptions | None = None
    ) -> "ModelCompletion":
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
    cached_input_tokens: int | None = None
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
        requires_key = self._requires_api_key()
        env_name = config.api_key_env or ("OPENAI_API_KEY" if requires_key else "")
        api_key = self._resolve_api_key(env_name) if env_name else None
        if not api_key and requires_key:
            raise RuntimeError(self._missing_api_key_message(env_name))
        self.api_key = api_key or ""

    def _requires_api_key(self) -> bool:
        return True

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
            except (
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
                ssl.SSLError,
                urllib.error.URLError,
            ) as exc:
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
        headers = {
            "Content-Type": "application/json",
            "User-Agent": HTTP_USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
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
    def complete_json(
        self, prompt: str, options: ModelCallOptions | None = None
    ) -> ModelCompletion:
        del options
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
    def complete_json(
        self, prompt: str, options: ModelCallOptions | None = None
    ) -> ModelCompletion:
        if not self.config.base_url:
            raise RuntimeError("openai_compatible_chat requires model.base_url")
        modes = self._response_format_modes()
        last_error: RuntimeError | None = None
        for mode in modes:
            payload = self._chat_payload(prompt, mode, options)
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

    def _chat_payload(
        self,
        prompt: str,
        response_format: str,
        options: ModelCallOptions | None = None,
    ) -> JsonDict:
        del options
        payload: JsonDict = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
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
                if text.strip():
                    return _completion_from_text(response, text, payload)
                raise ModelResponseFormatError(
                    "OpenAI-compatible API returned empty message content",
                    raw_response=response,
                    response_text=text,
                    request_payload=payload,
                )
        raise ModelResponseFormatError(
            "OpenAI-compatible API returned no message content",
            raw_response=response,
            response_text="",
            request_payload=payload,
        )


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
            response_format=config.response_format,
            max_tokens=config.max_tokens,
            context_window_tokens=config.context_window_tokens,
            cache_prompt=config.cache_prompt,
            id_slot=config.id_slot,
            thinking_mode=config.thinking_mode,
            no_thinking_action_threshold=config.no_thinking_action_threshold,
            timeout=config.timeout,
            retry_backoffs=config.retry_backoffs,
        )
        super().__init__(config)

    def complete_json(
        self, prompt: str, options: ModelCallOptions | None = None
    ) -> ModelCompletion:
        completion = super().complete_json(prompt, options)
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
            max_tokens=config.max_tokens,
            context_window_tokens=config.context_window_tokens,
            cache_prompt=config.cache_prompt,
            id_slot=config.id_slot,
            thinking_mode=config.thinking_mode,
            no_thinking_action_threshold=config.no_thinking_action_threshold,
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


class LlamaServerProvider(OpenAICompatibleChatProvider):
    def __init__(self, config: ModelConfig) -> None:
        config = ModelConfig(
            provider=config.provider,
            model=config.model or LLAMA_SERVER_DEFAULT_MODEL,
            api_key_env=config.api_key_env,
            base_url=config.base_url or LLAMA_SERVER_BASE_URL,
            response_format=config.response_format or "json_object",
            max_tokens=config.max_tokens or 20_000,
            context_window_tokens=config.context_window_tokens or 32_000,
            cache_prompt=True if config.cache_prompt is None else config.cache_prompt,
            id_slot=config.id_slot,
            thinking_mode=config.thinking_mode or "auto",
            no_thinking_action_threshold=config.no_thinking_action_threshold,
            timeout=config.timeout,
            retry_backoffs=config.retry_backoffs,
        )
        super().__init__(config)

    def _requires_api_key(self) -> bool:
        return bool(self.config.api_key_env)

    def _chat_payload(
        self,
        prompt: str,
        response_format: str,
        options: ModelCallOptions | None = None,
    ) -> JsonDict:
        payload = super()._chat_payload(prompt, response_format, options)
        if self.config.cache_prompt:
            payload["cache_prompt"] = True
        if self.config.id_slot is not None:
            payload["id_slot"] = self.config.id_slot
        if options is not None and options.thinking is not None:
            payload["chat_template_kwargs"] = {
                "enable_thinking": options.thinking,
            }
        return payload


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
    if config.provider == "llama_server":
        return LlamaServerProvider(config)
    raise ValueError(
        "model.provider must be openai_responses, openai_compatible_chat, "
        "opencode_go, openrouter, or llama_server"
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
    timings = response.get("timings")
    if not isinstance(timings, dict):
        timings = {}
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
        cached_input_tokens=(
            _usage_nested_int(usage, "input_tokens_details", "cached_tokens")
            or _usage_nested_int(usage, "prompt_tokens_details", "cached_tokens")
            or _usage_int(usage, "cached_input_tokens", "cached_prompt_tokens")
            or _usage_int(timings, "cache_n")
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
    response_text = completion.response_text
    if content_data:
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
        cached_input_tokens=completion.cached_input_tokens,
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


def _usage_nested_int(usage: JsonDict, *path: str) -> int | None:
    value: Any = usage
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
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
    prompt_snapshot, objective_battle_log = _snapshot_without_objective_battle_log(
        snapshot
    )
    prompt = template.replace(
        "{{SNAPSHOT_JSON}}", json.dumps(prompt_snapshot, indent=2, sort_keys=True)
    ).replace("{{MEMORY_JSON}}", json.dumps(memory, indent=2))
    lifecycle_requirement = build_lifecycle_requirement(snapshot, memory)
    if lifecycle_requirement:
        prompt = _insert_before_snapshot(
            prompt, f"\n\nLifecycle requirement:\n{lifecycle_requirement}\n"
        )
    ascension_requirement = build_ascension_effects_section(snapshot)
    if ascension_requirement:
        prompt = _insert_before_snapshot(prompt, f"\n\n{ascension_requirement}\n")
    if objective_battle_log is not None:
        prompt += (
            "\n\nObjective battle log:\n"
            f"{json.dumps(objective_battle_log, indent=2, sort_keys=True)}\n"
        )
    return prompt


def build_ascension_effects_section(snapshot: JsonDict) -> str | None:
    ascension = _snapshot_ascension(snapshot)
    if ascension is None:
        return None
    if ascension <= 0:
        return (
            "Active ascension effects:\n"
            "- Current ascension: A0.\n"
            "- No ascension modifiers are active."
        )
    active_level = min(ascension, len(ASCENSION_EFFECTS))
    lines = [
        "Active ascension effects:",
        f"- Current ascension: A{ascension}. Effects stack; all A1-A{active_level} entries below are active.",
    ]
    for index, (name, effect) in enumerate(ASCENSION_EFFECTS[:active_level], 1):
        lines.append(f"- A{index} {name}: {effect}")
    if ascension > len(ASCENSION_EFFECTS):
        lines.append(
            f"- A{len(ASCENSION_EFFECTS) + 1}+ effects are not known to this harness; "
            "trust explicit ascension data in the snapshot if present."
        )
    return "\n".join(lines)


def _snapshot_ascension(snapshot: JsonDict) -> int | None:
    for value in _ascension_candidates(snapshot):
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _ascension_candidates(snapshot: JsonDict) -> list[Any]:
    candidates: list[Any] = [snapshot.get("ascension")]
    for key in ("state", "run", "run_setup"):
        value = snapshot.get(key)
        if isinstance(value, dict):
            _append_ascension_candidates(candidates, value)
    verification = snapshot.get("run_setup_verification")
    if isinstance(verification, dict):
        for key in ("actual", "expected"):
            value = verification.get(key)
            if isinstance(value, dict):
                _append_ascension_candidates(candidates, value)
    actions = snapshot.get("actions")
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            request = action.get("request")
            if isinstance(request, dict):
                candidates.append(request.get("ascension"))
    return candidates


def _append_ascension_candidates(candidates: list[Any], data: JsonDict) -> None:
    for key in ("ascension", "ascension_level", "ascensionLevel"):
        candidates.append(data.get(key))
    for key in ("run", "current_run", "run_setup"):
        nested = data.get(key)
        if isinstance(nested, dict):
            _append_ascension_candidates(candidates, nested)


def _insert_before_snapshot(prompt: str, text: str) -> str:
    snapshot_start = _snapshot_section_start(prompt)
    if snapshot_start is None:
        return prompt + text
    return prompt[:snapshot_start] + text + prompt[snapshot_start:]


def _snapshot_section_start(prompt: str) -> int | None:
    for marker in ("\nSnapshot:\n\n", "\nSnapshot:\n"):
        snapshot_start = prompt.rfind(marker)
        if snapshot_start != -1:
            return snapshot_start
    return None


def _snapshot_without_objective_battle_log(
    snapshot: JsonDict,
) -> tuple[JsonDict, Any | None]:
    if "objective_battle_log" not in snapshot:
        return snapshot, None
    prompt_snapshot = dict(snapshot)
    return prompt_snapshot, prompt_snapshot.pop("objective_battle_log")


def _estimated_tokens(text: str) -> int:
    return max(1, len(text) // 2)


def _state_has_active_battle(state: JsonDict) -> bool:
    if state.get("state_type") == "monster":
        return True
    battle = state.get("battle")
    return isinstance(battle, dict) and state.get("state_type") == "hand_select"


def context_role_for_snapshot(snapshot: JsonDict, memory: JsonDict) -> str:
    state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
    if _state_has_active_battle(state):
        return "battle_tactician"
    if _snapshot_has_finished_battle(snapshot) and _memory_file_has_active_notes(
        memory.get("BATTLE_LOG.md"), "BATTLE_LOG.md"
    ):
        return "battle_tactician"
    return "map_pather"


def context_compaction_target(role: str) -> str:
    if role == "battle_tactician":
        return "BATTLE_LOG.md"
    return "CURRENT_RUN.md"


def build_context_compaction_requirement(role: str, target_path: str) -> str:
    if role == "battle_tactician":
        return (
            "- The battle tactician context is near its token limit. Before taking "
            "the next combat action, rewrite `BATTLE_LOG.md` as a compact but "
            "complete tactical state for this battle: enemy intents, HP/block "
            "math, cards played this turn, hand/draw/discard facts that matter, "
            "potion/lethal plans, and lessons needed to continue the fight. The "
            "harness will then drop older battle context and keep only the most "
            "recent decision plus the memory files injected in future prompts."
        )
    return (
        f"- The strategic map/pathing context is near its token limit. Before "
        f"taking the next non-combat action, rewrite `{target_path}` as a compact "
        "but complete active run state: seed/ascension/floor, HP/gold, deck and "
        "upgrades, relics, potions, route/boss/reward plan, risks, and recent "
        "decisions needed to continue the run. The harness will then drop older "
        "strategic context and keep only the most recent decision plus the memory "
        "files injected in future prompts."
    )


@dataclass
class AgentConversation:
    role: str
    exchanges: list[JsonDict]
    pending_compaction_path: str | None = None
    pending_compaction_prompted: bool = False

    def transcript(self) -> str:
        if not self.exchanges:
            return ""
        parts = [
            "Prior context for this same agent role. Treat this as your own "
            "conversation history, but the current Snapshot and Memory below are "
            "authoritative when they conflict.\n"
        ]
        for index, exchange in enumerate(self.exchanges, 1):
            parts.append(f"\n## Prior decision {index}\n")
            parts.append("Prompt:\n")
            parts.append(str(exchange.get("prompt") or ""))
            parts.append("\nDecision JSON:\n")
            parts.append(str(exchange.get("response_text") or ""))
            result = exchange.get("result_summary")
            if result is not None:
                parts.append("\nResult summary:\n")
                parts.append(json.dumps(result, indent=2, sort_keys=True))
            parts.append("\n")
        return "".join(parts)

    def estimated_tokens_with(self, prompt: str) -> int:
        return _estimated_tokens(self.transcript() + prompt)

    def prune_history_to_fit(self, prompt: str, token_threshold: int) -> int:
        pruned = 0
        while self.exchanges and self.estimated_tokens_with(prompt) >= token_threshold:
            del self.exchanges[0]
            pruned += 1
        return pruned

    def append_exchange(
        self, prompt: str, completion: ModelCompletion, result_summary: JsonDict
    ) -> None:
        self.exchanges.append(
            {
                "prompt": prompt,
                "response_text": completion.response_text,
                "decision": completion.decision,
                "result_summary": result_summary,
            }
        )

    def compact_to_recent_exchange(self) -> None:
        if self.exchanges:
            self.exchanges = [self.exchanges[-1]]
        self.pending_compaction_path = None
        self.pending_compaction_prompted = False

    def reset(self) -> None:
        self.exchanges = []
        self.pending_compaction_path = None
        self.pending_compaction_prompted = False


class AgentContextManager:
    def __init__(self, config: AgentContextConfig) -> None:
        self.config = config
        self.conversations: dict[str, AgentConversation] = {
            "map_pather": AgentConversation("map_pather", []),
            "battle_tactician": AgentConversation("battle_tactician", []),
        }

    def conversation_for(self, role: str) -> AgentConversation:
        return self.conversations[role]

    def reset_all(self) -> None:
        for conversation in self.conversations.values():
            conversation.reset()

    def build_prompt(
        self, base_prompt: str, role: str
    ) -> tuple[str, str, AgentConversation, JsonDict]:
        conversation = self.conversation_for(role)
        current_prompt = self._with_role_header(base_prompt, role)
        compaction_path = conversation.pending_compaction_path
        compaction_triggered = False
        history_pruned_exchanges = 0
        estimated_tokens = conversation.estimated_tokens_with(current_prompt)
        if (
            compaction_path is None
            and estimated_tokens >= self.config.compaction_token_threshold
        ):
            compaction_path = context_compaction_target(role)
            conversation.pending_compaction_path = compaction_path
            conversation.pending_compaction_prompted = True
            compaction_triggered = True
        if compaction_path is not None:
            current_prompt = _insert_before_snapshot(
                current_prompt,
                "\n\nContext compaction requirement:\n"
                f"{build_context_compaction_requirement(role, compaction_path)}\n",
            )
            history_pruned_exchanges = conversation.prune_history_to_fit(
                current_prompt, self.config.compaction_token_threshold
            )
        transcript = conversation.transcript()
        prompt = self._with_role_header(
            _prompt_with_history_before_current_prompt(base_prompt, transcript), role
        )
        if compaction_path is not None:
            prompt = _insert_before_snapshot(
                prompt,
                "\n\nContext compaction requirement:\n"
                f"{build_context_compaction_requirement(role, compaction_path)}\n",
            )
        cache_prefix = self._prompt_cache_prefix(role)
        prompt = cache_prefix + prompt
        return (
            prompt,
            cache_prefix + current_prompt,
            conversation,
            {
                "role": role,
                "enabled": True,
                "prompt_cache": self.config.prompt_cache,
                "estimated_tokens": _estimated_tokens(prompt),
                "history_exchanges": len(conversation.exchanges),
                "history_pruned_exchanges": history_pruned_exchanges,
                "compaction_path": compaction_path,
                "compaction_triggered": compaction_triggered,
            },
        )

    def _prompt_cache_prefix(self, role: str) -> str:
        if not self.config.prompt_cache:
            return ""
        return (
            f"Agent role: {role}\n"
            "Prompt-cache hint: this prompt keeps stable role instructions and "
            "prior same-role context before volatile current memory and snapshot "
            "data so providers with automatic prompt caching can reuse the "
            "unchanged prefix.\n\n"
        )

    def _with_role_header(self, prompt: str, role: str) -> str:
        if role == "battle_tactician":
            header = (
                "You are the battle tactician sub-agent for the current battle. "
                "Keep tactical continuity across combat decisions. During combat, "
                "prefer `BATTLE_LOG.md` for compact tactical state; when the battle "
                "is finished, fold useful battle results into `CURRENT_RUN.md` as "
                "required by lifecycle instructions.\n\n"
            )
        else:
            header = (
                "You are the outer map pather and strategic agent. Keep route, "
                "reward, event, shop, rest-site, and run-level strategy continuity. "
                "When entering combat, your context is preserved while the battle "
                "tactician sub-agent handles combat.\n\n"
            )
        return header + prompt


def _prompt_with_history_before_current_prompt(
    base_prompt: str, history: str
) -> str:
    if not history:
        return base_prompt
    return f"Agent history:\n{history}\n\nCurrent decision prompt:\n{base_prompt}"


def restore_agent_context_from_trace(
    manager: AgentContextManager, config: OrchestratorConfig
) -> JsonDict:
    sqlite_path = _trace_sqlite_path(config)
    if sqlite_path is None or not sqlite_path.exists():
        return {"restored": False, "reason": "trace sqlite not found"}
    restored = 0
    skipped = 0
    try:
        with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as conn:
            latest_run = conn.execute(
                """
                select run_id
                  from steps
                 where prompt_text is not null
                   and response_text is not null
                 order by id desc
                 limit 1
                """
            ).fetchone()
            if latest_run is None:
                return {
                    "restored": True,
                    "sqlite_path": str(sqlite_path),
                    "run_id": None,
                    "exchanges": 0,
                    "skipped": 0,
                    "roles": {
                        role: len(conversation.exchanges)
                        for role, conversation in manager.conversations.items()
                    },
                }
            run_id = latest_run[0]
            rows = conn.execute(
                """
                select prompt_text, response_text, action_chosen, invalid_action_error,
                       observation_json, state_type, floor, hp, max_hp, gold
                  from steps
                 where run_id = ?
                   and prompt_text is not null
                   and response_text is not null
                 order by id
                """,
                (run_id,),
            ).fetchall()
    except sqlite3.Error as exc:
        return {"restored": False, "reason": f"trace sqlite read failed: {exc}"}
    for (
        prompt_text,
        response_text,
        action_chosen,
        invalid_error,
        observation_json,
        state_type,
        floor,
        hp,
        max_hp,
        gold,
    ) in rows:
        if not isinstance(prompt_text, str) or not isinstance(response_text, str):
            skipped += 1
            continue
        role = _role_from_prompt_text(prompt_text)
        if role is None:
            skipped += 1
            continue
        exchange_prompt = _restored_exchange_prompt_summary(
            observation_json, state_type, floor, hp, max_hp, gold
        )
        decision = _decision_from_response_text(response_text)
        result_summary = _result_summary_from_trace(action_chosen, invalid_error)
        manager.conversation_for(role).append_exchange(
            exchange_prompt,
            ModelCompletion(
                decision=decision,
                raw_response={},
                response_text=response_text,
                request_payload={},
            ),
            result_summary,
        )
        restored += 1
    return {
        "restored": True,
        "sqlite_path": str(sqlite_path),
        "run_id": run_id,
        "exchanges": restored,
        "skipped": skipped,
        "roles": {
            role: len(conversation.exchanges)
            for role, conversation in manager.conversations.items()
        },
    }


def _restored_exchange_prompt_summary(
    observation_json: Any,
    state_type: Any,
    floor: Any,
    hp: Any,
    max_hp: Any,
    gold: Any,
) -> str:
    parts = [
        "Restored historical decision from trace. The original full prompt is "
        "omitted on resume to keep context bounded; the current Snapshot and "
        "Memory in the live prompt are authoritative.\n"
    ]
    state_text = _optional_trace_value(state_type)
    if state_text is not None:
        parts.append(f"- state_type: {state_text}\n")
    floor_text = _optional_trace_value(floor)
    if floor_text is not None:
        parts.append(f"- floor: {floor_text}\n")
    if hp is not None or max_hp is not None:
        parts.append(f"- hp: {_optional_trace_value(hp) or '?'}")
        max_hp_text = _optional_trace_value(max_hp)
        if max_hp_text is not None:
            parts.append(f"/{max_hp_text}")
        parts.append("\n")
    gold_text = _optional_trace_value(gold)
    if gold_text is not None:
        parts.append(f"- gold: {gold_text}\n")
    actions = _actions_summary_from_observation_json(observation_json)
    if actions:
        parts.append("- legal_actions: ")
        parts.append("; ".join(actions))
        parts.append("\n")
    return "".join(parts)


def _optional_trace_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _actions_summary_from_observation_json(observation_json: Any) -> list[str]:
    if not isinstance(observation_json, str) or not observation_json:
        return []
    try:
        observation = json.loads(observation_json)
    except json.JSONDecodeError:
        return []
    actions = observation.get("actions") if isinstance(observation, dict) else None
    if not isinstance(actions, list):
        return []
    result: list[str] = []
    for action in actions[:20]:
        if not isinstance(action, dict):
            continue
        action_id = _optional_trace_value(action.get("id"))
        label = _optional_trace_value(action.get("label"))
        if action_id and label:
            result.append(f"{action_id} ({label})")
        elif action_id:
            result.append(action_id)
        elif label:
            result.append(label)
    if len(actions) > len(result):
        result.append(f"... {len(actions) - len(result)} more")
    return result


def _trace_sqlite_path(config: OrchestratorConfig) -> Path | None:
    path = _trace_sqlite_path_from_rpc_command(config.rpc_server_command)
    if path is not None:
        return path
    decision_log = Path(config.decision_log).expanduser()
    if decision_log.name:
        return decision_log.parent / "runs.sqlite"
    return None


def _trace_sqlite_path_from_rpc_command(command: list[str]) -> Path | None:
    try:
        config_index = command.index("--config") + 1
    except ValueError:
        return None
    if config_index >= len(command):
        return None
    try:
        pi_config = _load_json(command[config_index])
        harness_config_path = pi_config.get("harness_config")
        if not isinstance(harness_config_path, str):
            return None
        harness_config = _load_json(harness_config_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    logging_config = harness_config.get("logging")
    if not isinstance(logging_config, dict):
        return None
    sqlite_path = logging_config.get("sqlite_path")
    if not isinstance(sqlite_path, str) or not sqlite_path:
        return None
    return Path(sqlite_path).expanduser()


def _role_from_prompt_text(prompt_text: str) -> str | None:
    first_line = prompt_text.splitlines()[0] if prompt_text.splitlines() else ""
    match = re.match(r"Agent role: ([A-Za-z0-9_]+)", first_line)
    if match:
        role = match.group(1)
        if role in {"map_pather", "battle_tactician"}:
            return role
    if "You are the battle tactician sub-agent" in prompt_text:
        return "battle_tactician"
    if "You are the outer map pather" in prompt_text:
        return "map_pather"
    return None


def _current_exchange_prompt_from_trace(prompt_text: str) -> str:
    marker = "\n\nCurrent decision prompt:\n"
    if marker in prompt_text:
        return prompt_text.rsplit(marker, 1)[1]
    history_marker = "\n\nAgent history:\n"
    snapshot_marker = "\n\nSnapshot:\n"
    history_start = prompt_text.find(history_marker)
    if history_start == -1:
        return _strip_cache_and_role_header(prompt_text)
    snapshot_start = prompt_text.find(snapshot_marker, history_start + len(history_marker))
    if snapshot_start == -1:
        return _strip_cache_and_role_header(prompt_text[:history_start])
    return _strip_cache_and_role_header(
        prompt_text[:history_start] + prompt_text[snapshot_start:]
    )


def _strip_cache_and_role_header(prompt_text: str) -> str:
    text = prompt_text
    if text.startswith("Agent role: "):
        parts = text.split("\n\n", 2)
        if len(parts) == 3:
            text = parts[2]
    for header in (
        "You are the battle tactician sub-agent",
        "You are the outer map pather",
    ):
        index = text.find(header)
        if index == -1:
            continue
        end = text.find("\n\n", index)
        if end != -1:
            return text[end + 2 :]
    return text


def _decision_from_response_text(response_text: str) -> JsonDict:
    try:
        return _parse_json_text(response_text)
    except (json.JSONDecodeError, ValueError):
        return {"action_ref": "", "rationale": "historical response was invalid JSON"}


def _result_summary_from_trace(action_chosen: Any, invalid_error: Any) -> JsonDict:
    if isinstance(invalid_error, str) and invalid_error:
        return {"status": "rejected", "error": invalid_error}
    action = None
    if isinstance(action_chosen, str) and action_chosen:
        try:
            action = json.loads(action_chosen)
        except json.JSONDecodeError:
            action = action_chosen
    return {"status": "ok", "action": action}


def memory_bundle_for_context_role(memory: JsonDict, role: str) -> JsonDict:
    if role == "map_pather":
        allowed = ("STRATEGY.md", "CURRENT_RUN.md")
    elif role == "battle_tactician":
        allowed = ("STRATEGY.md", "CURRENT_RUN.md", "BATTLE_LOG.md")
    else:
        allowed = tuple(memory.keys())
    return {path: memory[path] for path in allowed if path in memory}


def validate_context_compaction(decision: JsonDict, conversation: AgentConversation) -> str | None:
    path = conversation.pending_compaction_path
    if path is None:
        return None
    if path in memory_update_write_paths(decision):
        return None
    return f"context compaction requires rewriting {path} before advancing"


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
    if _snapshot_has_completed_run(snapshot):
        return (
            "STRATEGY.md",
            "completed run requires a durable strategy revision",
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
            "whether it won or died, what card/relic/pathing/tactical choices "
            "mattered, and what to repeat or do differently in future runs. "
            "`STRATEGY.md` is your only persistent cross-run playbook, so do not "
            "make a sparse summary; "
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

    if _snapshot_has_completed_run(snapshot) and "STRATEGY.md" in written_paths:
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
                "reason": "strategy_rewritten_after_completed_run",
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
                    "reason": "strategy_rewritten_after_completed_run",
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


def _snapshot_has_completed_run(value: Any) -> bool:
    if isinstance(value, dict):
        progress = value.get("progress_update")
        if (
            isinstance(progress, dict)
            and isinstance(progress.get("last_completed_victory"), bool)
        ):
            return True
        return any(_snapshot_has_completed_run(child) for child in value.values())
    if isinstance(value, list):
        return any(_snapshot_has_completed_run(child) for child in value)
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
    provider: ModelProvider,
    config: OrchestratorConfig,
    prompt: str,
    step: int,
    options: ModelCallOptions | None = None,
) -> ModelCompletion:
    for attempt in range(MODEL_DECISION_PARSE_RETRIES + 1):
        try:
            return provider.complete_json(prompt, options)
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


def complete_with_decision_retries(
    provider: ModelProvider,
    config: OrchestratorConfig,
    prompt: str,
    step: int,
    options: ModelCallOptions | None,
    actions: list[JsonDict],
    snapshot: JsonDict,
    memory: JsonDict,
    conversation: AgentConversation | None,
) -> tuple[ModelCompletion, str, str | None, str, int]:
    current_prompt = prompt
    model_calls = 0
    for attempt in range(MODEL_DECISION_PARSE_RETRIES + 1):
        completion = complete_with_response_retries(
            provider, config, current_prompt, step, options
        )
        model_calls += 1
        action_ref, validation_error = validate_decision(
            completion.decision, actions, snapshot, memory, conversation
        )
        final_attempt = attempt >= MODEL_DECISION_PARSE_RETRIES
        if validation_error is None or final_attempt:
            return completion, action_ref, validation_error, current_prompt, model_calls
        append_decision_log(
            config.decision_log,
            {
                "timestamp": time.time(),
                "step": step,
                "event": "invalid_model_decision",
                "attempt": attempt + 1,
                "max_attempts": MODEL_DECISION_PARSE_RETRIES + 1,
                "error": validation_error,
                "decision": completion.decision,
                "action_ref": action_ref,
                "legal_actions": _legal_actions_for_retry(actions),
                "prompt_hash": _sha256_text(current_prompt),
                "response_hash": _sha256_json(completion.raw_response),
                "final_attempt": False,
            },
        )
        current_prompt = _retry_prompt_for_invalid_decision(
            prompt,
            validation_error,
            completion.decision,
            actions,
        )
        time.sleep(MODEL_DECISION_PARSE_RETRY_DELAY)
    raise RuntimeError("unreachable decision retry state")


def validate_decision(
    decision: JsonDict,
    actions: list[JsonDict],
    snapshot: JsonDict,
    memory: JsonDict,
    conversation: AgentConversation | None,
) -> tuple[str, str | None]:
    action_ref = str(decision.get("action_ref") or "")
    try:
        action_ref = validate_action(decision, actions)
    except ValueError as exc:
        return action_ref, str(exc)
    validation_error = validate_memory_lifecycle(decision, snapshot, memory)
    if validation_error is not None:
        return action_ref, validation_error
    if conversation is not None:
        validation_error = validate_context_compaction(decision, conversation)
        if validation_error is not None:
            return action_ref, validation_error
    validation_error = validate_memory_quality(decision, snapshot, memory)
    return action_ref, validation_error


def _retry_prompt_for_invalid_decision(
    prompt: str,
    validation_error: str,
    decision: JsonDict,
    actions: list[JsonDict],
) -> str:
    correction = {
        "validation_error": validation_error,
        "previous_decision": decision,
        "legal_actions": _legal_actions_for_retry(actions),
    }
    return (
        prompt
        + "\n\nCorrection required:\n"
        + "Your previous decision was rejected by the harness. Return corrected "
        + "decision JSON only. Keep required memory rewrites if the error asks for "
        + "them, and choose exactly one legal action_ref from the legal_actions list.\n"
        + json.dumps(correction, indent=2, sort_keys=True)
        + "\n"
    )


def _legal_actions_for_retry(actions: list[JsonDict]) -> list[JsonDict]:
    result: list[JsonDict] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        item: JsonDict = {}
        for key in ("index", "id", "label", "category", "enabled"):
            if key in action:
                item[key] = action[key]
        result.append(item)
    return result


def _context_size_error(exc: RuntimeError) -> bool:
    text = str(exc).lower()
    return (
        "exceed_context_size_error" in text
        or "exceeds the available context size" in text
    )


def model_call_options_for_actions(
    model: ModelConfig, actions: list[JsonDict]
) -> ModelCallOptions:
    if model.provider != "llama_server":
        return ModelCallOptions()
    mode = model.thinking_mode or "auto"
    if mode == "enabled":
        return ModelCallOptions(thinking=True)
    if mode == "disabled":
        return ModelCallOptions(thinking=False)
    if mode == "auto":
        return ModelCallOptions(
            thinking=len(actions) > model.no_thinking_action_threshold
        )
    return ModelCallOptions()


def _model_call_options_record(options: ModelCallOptions) -> JsonDict:
    return {"thinking": options.thinking}


def run(config: OrchestratorConfig) -> int:
    provider = make_provider(config.model)
    context_manager = (
        AgentContextManager(config.agent_context)
        if config.agent_context.enabled
        else None
    )
    rpc = JsonRpcClient(config.rpc_server_command)
    try:
        rpc.call("ping", {})
        step = next_decision_step(config.decision_log)
        if context_manager is not None and step > 0:
            restore_record = restore_agent_context_from_trace(context_manager, config)
            append_decision_log(
                config.decision_log,
                {
                    "timestamp": time.time(),
                    "step": step,
                    "event": "agent_context_restored",
                    "agent_context_restore": restore_record,
                },
            )
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
            context_record: JsonDict = {"enabled": False}
            conversation: AgentConversation | None = None
            role = "single_agent"
            if context_manager is not None:
                role = context_role_for_snapshot(snapshot, memory)
                prompt_memory = memory_bundle_for_context_role(memory, role)
            else:
                prompt_memory = memory
            base_prompt = build_prompt(config.prompt_template, snapshot, prompt_memory)
            exchange_prompt = base_prompt
            if context_manager is not None:
                (
                    prompt,
                    exchange_prompt,
                    conversation,
                    context_record,
                ) = context_manager.build_prompt(base_prompt, role)
            else:
                prompt = base_prompt
            model_call_options = model_call_options_for_actions(config.model, actions)
            model_started = time.monotonic()
            try:
                (
                    completion,
                    action_ref,
                    validation_error,
                    prompt,
                    model_calls,
                ) = complete_with_decision_retries(
                    provider,
                    config,
                    prompt,
                    step,
                    model_call_options,
                    actions,
                    snapshot,
                    memory,
                    conversation,
                )
            except RuntimeError as exc:
                if (
                    context_manager is None
                    or conversation is None
                    or not _context_size_error(exc)
                ):
                    raise
                pruned = len(conversation.exchanges)
                conversation.exchanges = []
                if conversation.pending_compaction_path is None:
                    conversation.pending_compaction_path = context_compaction_target(role)
                    conversation.pending_compaction_prompted = True
                append_decision_log(
                    config.decision_log,
                    {
                        "timestamp": time.time(),
                        "step": step,
                        "event": "context_size_retry",
                        "error": str(exc),
                        "role": role,
                        "history_pruned_exchanges": pruned,
                    },
                )
                (
                    prompt,
                    exchange_prompt,
                    conversation,
                    context_record,
                ) = context_manager.build_prompt(base_prompt, role)
                context_record["context_size_retry"] = True
                context_record["history_pruned_exchanges"] = (
                    int(context_record.get("history_pruned_exchanges") or 0) + pruned
                )
                (
                    completion,
                    action_ref,
                    validation_error,
                    prompt,
                    model_calls,
                ) = complete_with_decision_retries(
                    provider,
                    config,
                    prompt,
                    step,
                    model_call_options,
                    actions,
                    snapshot,
                    memory,
                    conversation,
                )
            model_elapsed_seconds = time.monotonic() - model_started
            decision = completion.decision
            memory_update_count = 0
            memory_preread_count = 0
            memory_lifecycle: list[JsonDict] = []
            act_count = 0
            written_paths: set[str] = set()
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
            result_summary = _result_summary(result)
            if conversation is not None:
                conversation.append_exchange(
                    exchange_prompt, completion, result_summary
                )
                if (
                    validation_error is None
                    and conversation.pending_compaction_path is not None
                    and conversation.pending_compaction_path in written_paths
                ):
                    conversation.compact_to_recent_exchange()
                    context_record["compacted"] = True
                else:
                    context_record["compacted"] = False
                if (
                    role == "battle_tactician"
                    and validation_error is None
                    and "CURRENT_RUN.md" in written_paths
                    and _snapshot_has_finished_battle(snapshot)
                ):
                    conversation.reset()
                    context_record["battle_context_reset"] = True
                if (
                    context_manager is not None
                    and validation_error is None
                    and "STRATEGY.md" in written_paths
                    and _snapshot_has_completed_run(snapshot)
                ):
                    context_manager.reset_all()
                    context_record["all_context_reset"] = True
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
                    "cached_input_tokens": completion.cached_input_tokens,
                    "output_tokens": completion.output_tokens,
                    "model_elapsed_seconds": model_elapsed_seconds,
                    "tool_calls": tool_calls,
                    "model_calls": model_calls,
                    "model_call_options": _model_call_options_record(
                        model_call_options
                    ),
                    "agent_context": context_record,
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
                        "cached_input_tokens": completion.cached_input_tokens,
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
                    "model_calls": model_calls,
                    "result_summary": result_summary,
                    "memory_lifecycle": memory_lifecycle,
                    "model_call_options": _model_call_options_record(
                        model_call_options
                    ),
                    "agent_context": context_record,
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
