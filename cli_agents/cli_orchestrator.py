from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class CliOrchestratorConfig:
    harness_command: list[str]
    agent_command: list[str]
    resume_command: list[str]
    workspace: str
    prompt_path: str
    log_path: str
    max_iterations: int = 200
    stop_on_game_over: bool = True
    timeout: float = 900.0


def _load_json(path: str | Path) -> JsonDict:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def load_config(path: str | Path) -> CliOrchestratorConfig:
    raw = _load_json(path)
    harness = raw.get("harness_command")
    agent = raw.get("agent_command")
    resume = raw.get("resume_command") or agent
    if not isinstance(harness, list) or not harness:
        raise ValueError("harness_command must be a non-empty list")
    if not isinstance(agent, list) or not agent:
        raise ValueError("agent_command must be a non-empty list")
    if not isinstance(resume, list) or not resume:
        raise ValueError("resume_command must be a non-empty list")
    return CliOrchestratorConfig(
        harness_command=[str(part) for part in harness],
        agent_command=[str(part) for part in agent],
        resume_command=[str(part) for part in resume],
        workspace=str(raw["workspace"]),
        prompt_path=str(raw["prompt_path"]),
        log_path=str(raw["log_path"]),
        max_iterations=int(raw.get("max_iterations", 200)),
        stop_on_game_over=bool(raw.get("stop_on_game_over", True)),
        timeout=float(raw.get("timeout", 900.0)),
    )


def run_json(command: list[str], *, cwd: str | None = None, timeout: float = 60.0) -> JsonDict:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{command!r} returned non-JSON stdout with exit {result.returncode}: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{command!r} returned non-object JSON")
    if result.returncode != 0:
        parsed.setdefault("status", "error")
        parsed.setdefault("returncode", result.returncode)
    return parsed


def snapshot(config: CliOrchestratorConfig) -> JsonDict:
    return run_json(config.harness_command + ["snapshot"], timeout=90.0)


def run_agent(command: list[str], config: CliOrchestratorConfig) -> JsonDict:
    prompt = Path(config.prompt_path).read_text(encoding="utf-8")
    result = subprocess.run(
        command,
        cwd=config.workspace,
        input=prompt,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config.timeout,
    )
    return {
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-8000:],
        "stderr_tail": result.stderr[-8000:],
    }


def append_log(path: str, record: JsonDict) -> None:
    log_path = Path(path).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def run(config: CliOrchestratorConfig) -> int:
    Path(config.workspace).mkdir(parents=True, exist_ok=True)
    for iteration in range(config.max_iterations):
        before = snapshot(config)
        before_state = before.get("state") if isinstance(before.get("state"), dict) else {}
        if config.stop_on_game_over and before_state.get("state_type") == "game_over":
            append_log(
                config.log_path,
                {
                    "timestamp": time.time(),
                    "iteration": iteration,
                    "event": "game_over_before_agent",
                    "snapshot": before,
                },
            )
            return 0

        command = config.agent_command if iteration == 0 else config.resume_command
        agent_result = run_agent(command, config)
        after = snapshot(config)
        append_log(
            config.log_path,
            {
                "timestamp": time.time(),
                "iteration": iteration,
                "command": command,
                "before": summarize_snapshot(before),
                "agent": agent_result,
                "after": summarize_snapshot(after),
            },
        )

        after_state = after.get("state") if isinstance(after.get("state"), dict) else {}
        if config.stop_on_game_over and after_state.get("state_type") == "game_over":
            return 0
    return 0


def summarize_snapshot(value: JsonDict) -> JsonDict:
    state = value.get("state") if isinstance(value.get("state"), dict) else {}
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    return {
        "state_type": state.get("state_type"),
        "floor": state.get("floor") or run.get("floor"),
        "hp": player.get("hp"),
        "gold": player.get("gold"),
        "actions_count": len(value.get("actions") or []),
        "auto_actions_count": len(value.get("auto_actions") or []),
        "progress_update": value.get("progress_update"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supervise a CLI agent with snapshot checks between resume calls."
    )
    parser.add_argument("--config", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(load_config(args.config))


if __name__ == "__main__":
    raise SystemExit(main())
