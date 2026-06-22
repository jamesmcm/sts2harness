#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print live status for an OpenCode Go trial directory."
    )
    parser.add_argument("trial_root", type=Path)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=60.0)
    parser.add_argument("--tail-existing", type=int, default=5)
    args = parser.parse_args()

    trial_root = args.trial_root.resolve()
    decision_log = trial_root / "decisions.jsonl"
    sqlite_path = trial_root / "runs.sqlite"

    print(
        f"[monitor] watching {decision_log} and {sqlite_path}",
        flush=True,
    )

    offset = 0
    if decision_log.exists():
        lines = decision_log.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-max(args.tail_existing, 0) :]:
            record = _parse_json_line(line)
            if record is not None:
                _print_decision(record, historical=True)
        offset = decision_log.stat().st_size

    repeated_rejections: dict[str, int] = {}
    last_heartbeat = 0.0
    last_step: int | None = None

    while True:
        now = time.monotonic()
        if decision_log.exists():
            size = decision_log.stat().st_size
            if size < offset:
                print("[monitor] decision log was truncated; restarting tail", flush=True)
                offset = 0
            if size > offset:
                with decision_log.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
                for line in chunk.splitlines():
                    record = _parse_json_line(line)
                    if record is None:
                        continue
                    step = record.get("step")
                    if isinstance(step, int):
                        last_step = step
                    _print_decision(record, historical=False)
                    _track_rejection(record, repeated_rejections)

        if now - last_heartbeat >= args.heartbeat_seconds:
            _print_sqlite_heartbeat(sqlite_path, last_step)
            last_heartbeat = now

        time.sleep(args.poll_seconds)


def _parse_json_line(line: str) -> JsonDict | None:
    if not line.strip():
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        print(f"[monitor] invalid jsonl line: {exc}", flush=True)
        return None
    return value if isinstance(value, dict) else None


def _print_decision(record: JsonDict, *, historical: bool) -> None:
    prefix = "[monitor:recent]" if historical else "[monitor]"
    step = record.get("step", "?")
    if record.get("event") == "game_over":
        state = _nested_get(record, ("snapshot", "state", "state_type"))
        print(f"{prefix} step={step} event=game_over state={state}", flush=True)
        return

    result = record.get("result_summary")
    result = result if isinstance(result, dict) else {}
    telemetry = record.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    usage = record.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    status = result.get("status") or "?"
    state_type = result.get("state_type") or "?"
    action = result.get("action")
    if isinstance(action, dict):
        action_text = action.get("id") or action.get("label") or action.get("request")
    else:
        action_text = action or record.get("action_ref") or "?"
    error = record.get("validation_error") or result.get("error")
    tool_calls = record.get("tool_calls")
    tool_total = (
        tool_calls.get("total")
        if isinstance(tool_calls, dict) and tool_calls.get("total") is not None
        else "?"
    )
    tokens = usage.get("total_tokens") or "?"
    output_tps = _optional_float(record.get("tokens_per_second"))
    total_tps = _optional_float(record.get("total_tokens_per_second"))
    run_id = telemetry.get("run_id") or "?"
    reason = telemetry.get("reason")

    parts = [
        f"{prefix} step={step}",
        f"status={status}",
        f"state={state_type}",
        f"action={_short(action_text, 90)}",
        f"tokens={tokens}",
        f"output_tps={_format_rate(output_tps)}",
        f"total_tps={_format_rate(total_tps)}",
        f"tools={tool_total}",
    ]
    if reason:
        parts.append(f"telemetry={reason}")
    if run_id != "?":
        parts.append(f"run={_short(run_id, 80)}")
    if error:
        parts.append(f"error={_short(error, 180)}")
    print(" ".join(str(part) for part in parts), flush=True)


def _track_rejection(record: JsonDict, repeated_rejections: dict[str, int]) -> None:
    result = record.get("result_summary")
    result = result if isinstance(result, dict) else {}
    error = record.get("validation_error") or result.get("error")
    if result.get("status") != "rejected" or not error:
        repeated_rejections.clear()
        return
    key = str(error)
    repeated_rejections[key] = repeated_rejections.get(key, 0) + 1
    for other in list(repeated_rejections):
        if other != key:
            del repeated_rejections[other]
    count = repeated_rejections[key]
    if count in {3, 5} or (count > 5 and count % 10 == 0):
        print(
            f"[monitor:warning] same rejection repeated {count} times: "
            f"{_short(key, 220)}",
            flush=True,
        )


def _print_sqlite_heartbeat(sqlite_path: Path, last_step: int | None) -> None:
    if not sqlite_path.exists():
        print("[monitor] heartbeat: sqlite not created yet", flush=True)
        return
    try:
        with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            run_columns = _table_columns(conn, "runs")
            elapsed_expr = (
                "total_model_elapsed_seconds"
                if "total_model_elapsed_seconds" in run_columns
                else "NULL"
            )
            runs = conn.execute(
                f"""
                select run_id, seed, ascension, start_time, end_time, final_floor,
                       act, victory, agent_actions, auto_actions, total_model_calls,
                       total_input_tokens, total_output_tokens,
                       {elapsed_expr} as total_model_elapsed_seconds
                  from runs
                 order by start_time desc
                 limit 1
                """
            ).fetchone()
            completed = conn.execute(
                "select count(*) from runs where end_time is not null"
            ).fetchone()[0]
            latest_step = conn.execute(
                """
                select floor, state_type, hp, max_hp, timestamp
                  from steps
                 order by id desc
                 limit 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        print(f"[monitor] heartbeat: sqlite read failed: {exc}", flush=True)
        return

    if runs is None:
        print("[monitor] heartbeat: no runs logged yet", flush=True)
        return

    status = "complete" if runs["end_time"] else "open"
    elapsed = _optional_float(runs["total_model_elapsed_seconds"])
    output_tokens = _optional_int(runs["total_output_tokens"])
    input_tokens = _optional_int(runs["total_input_tokens"])
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    step_bits = ""
    if latest_step is not None:
        hp = ""
        if latest_step["hp"] is not None and latest_step["max_hp"] is not None:
            hp = f" hp={latest_step['hp']}/{latest_step['max_hp']}"
        step_bits = (
            f" last_state={latest_step['state_type']}"
            f" floor={latest_step['floor']}{hp}"
        )
    print(
        "[monitor] heartbeat:"
        f" completed_runs={completed}"
        f" latest_run={_short(runs['run_id'], 80)}"
        f" status={status}"
        f" final_floor={runs['final_floor']}"
        f" act={runs['act']}"
        f" victory={runs['victory']}"
        f" agent_actions={runs['agent_actions']}"
        f" auto_actions={runs['auto_actions']}"
        f" model_calls={runs['total_model_calls']}"
        f" output_tps={_format_rate(_rate(output_tokens, elapsed))}"
        f" total_tps={_format_rate(_rate(total_tokens, elapsed))}"
        f" decision_step={last_step if last_step is not None else '?'}"
        f"{step_bits}",
        flush=True,
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate(tokens: int | None, elapsed_seconds: float | None) -> float | None:
    if tokens is None or elapsed_seconds is None or elapsed_seconds <= 0:
        return None
    return tokens / elapsed_seconds


def _format_rate(value: float | None) -> str:
    return "?" if value is None else f"{value:.2f}"


def _nested_get(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for item in path:
        if not isinstance(current, dict):
            return None
        current = current.get(item)
    return current


def _short(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
