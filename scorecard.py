#!/usr/bin/env python3
"""Agent trace scorecard CLI.

Reads JSONL agent traces and computes reliability/efficiency metrics:
- command_count
- unique_command_count
- repeated_command_count
- thrash_ratio
- retry_event_count
- replayed_execution_count / replay_rate
- missing_side_effect_ref_count
- stale execution detection + recovery latency

Usage:
  python3 scorecard.py trace.jsonl --max-thrash 0.35
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
from typing import Any


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                events.append(obj)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at line {idx}: {exc}") from exc
    return events


def _event_kind(event: dict[str, Any]) -> str:
    for key in ("type", "event", "kind", "name"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    return "unknown"


def _extract_command(event: dict[str, Any]) -> str | None:
    if isinstance(event.get("command"), str):
        return event["command"].strip()

    args = event.get("arguments")
    if isinstance(args, dict):
        for key in ("command", "cmd", "shell"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _extract_duration_ms(event: dict[str, Any]) -> float | None:
    candidates = [event.get("duration_ms")]
    args = event.get("arguments")
    if isinstance(args, dict):
        candidates.append(args.get("duration_ms"))

    for value in candidates:
        if isinstance(value, (int, float)) and value >= 0:
            return float(value)
    return None


def _extract_timestamp(event: dict[str, Any]) -> datetime | None:
    for key in ("created_at", "timestamp", "time", "ts"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            # Heuristic: values > 1e11 are usually milliseconds.
            epoch = float(value / 1000.0) if value > 1e11 else float(value)
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _is_failure_event(event: dict[str, Any]) -> bool:
    kind = _event_kind(event)
    if "retry" in kind:
        return True
    if any(token in kind for token in ("error", "fail", "timeout", "exception")):
        return True

    for key in ("status", "result", "outcome"):
        value = event.get(key)
        if isinstance(value, str) and value.lower() in {"error", "failed", "failure", "timeout"}:
            return True

    if event.get("success") is False:
        return True

    return False


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    args = event.get("arguments")
    if isinstance(args, dict):
        nested = args.get("payload")
        if isinstance(nested, dict):
            return nested
    return {}


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    commands: list[str] = []
    retry_count = 0
    durations_ms: list[float] = []
    failure_count = 0
    command_outcomes: dict[str, set[str]] = {}
    executed_action_count = 0
    replayed_execution_count = 0
    missing_side_effect_ref_count = 0
    stale_detection_count = 0
    stale_detected_at: dict[str, datetime] = {}
    stale_recovery_latencies_ms: list[float] = []

    for event in events:
        kind = _event_kind(event)
        if "retry" in kind:
            retry_count += 1

        failed = _is_failure_event(event)
        if failed:
            failure_count += 1

        duration = _extract_duration_ms(event)
        if duration is not None:
            durations_ms.append(duration)

        if kind in {"action.execution.stale_detected", "action_execution_stale_detected"}:
            stale_detection_count += 1
            action_id = event.get("action_id")
            event_ts = _extract_timestamp(event)
            if isinstance(action_id, str) and event_ts is not None and action_id not in stale_detected_at:
                stale_detected_at[action_id] = event_ts

        if kind in {"action.executed", "action_executed"}:
            executed_action_count += 1
            payload = _payload(event)
            if payload.get("replayed") is True:
                replayed_execution_count += 1
            if "side_effect_ref" not in payload or not payload.get("side_effect_ref"):
                missing_side_effect_ref_count += 1

            action_id = event.get("action_id")
            event_ts = _extract_timestamp(event)
            if isinstance(action_id, str) and event_ts is not None and action_id in stale_detected_at:
                latency_ms = (event_ts - stale_detected_at[action_id]).total_seconds() * 1000.0
                if latency_ms >= 0:
                    stale_recovery_latencies_ms.append(latency_ms)

        cmd = _extract_command(event)
        if cmd:
            commands.append(cmd)
            outcome = "failure" if failed else "success"
            command_outcomes.setdefault(cmd, set()).add(outcome)

    command_count = len(commands)
    unique_count = len(set(commands))
    repeated_count = command_count - unique_count
    thrash_ratio = (repeated_count / command_count) if command_count else 0.0
    failure_rate = (failure_count / len(events)) if events else 0.0

    top_repeats = Counter(commands).most_common(5)

    sorted_lat = sorted(durations_ms)
    p50_idx = (len(sorted_lat) // 2) if sorted_lat else 0
    p95_idx = (ceil(0.95 * len(sorted_lat)) - 1) if sorted_lat else 0
    latency = {
        "samples": len(durations_ms),
        "p50_ms": round(float(sorted_lat[p50_idx]), 2) if sorted_lat else None,
        "p95_ms": round(float(sorted_lat[p95_idx]), 2) if sorted_lat else None,
    }
    flaky_command_count = sum(1 for outcomes in command_outcomes.values() if len(outcomes) > 1)

    replay_rate = (replayed_execution_count / executed_action_count) if executed_action_count else 0.0
    flaky_command_rate = (flaky_command_count / unique_count) if unique_count else 0.0

    stale_recovered_count = len(stale_recovery_latencies_ms)
    unresolved_stale_count = max(0, stale_detection_count - stale_recovered_count)
    unresolved_stale_rate = (unresolved_stale_count / stale_detection_count) if stale_detection_count else 0.0

    stale_recovery_latencies_ms.sort()
    stale_latency_p50 = None
    stale_latency_p95 = None
    if stale_recovery_latencies_ms:
        idx50 = len(stale_recovery_latencies_ms) // 2
        idx95 = ceil(0.95 * len(stale_recovery_latencies_ms)) - 1
        stale_latency_p50 = round(float(stale_recovery_latencies_ms[idx50]), 2)
        stale_latency_p95 = round(float(stale_recovery_latencies_ms[max(0, idx95)]), 2)

    return {
        "event_count": len(events),
        "command_count": command_count,
        "unique_command_count": unique_count,
        "repeated_command_count": repeated_count,
        "retry_event_count": retry_count,
        "failure_event_count": failure_count,
        "failure_rate": round(failure_rate, 4),
        "flaky_command_count": flaky_command_count,
        "flaky_command_rate": round(flaky_command_rate, 4),
        "thrash_ratio": round(thrash_ratio, 4),
        "executed_action_count": executed_action_count,
        "replayed_execution_count": replayed_execution_count,
        "replay_rate": round(replay_rate, 4),
        "missing_side_effect_ref_count": missing_side_effect_ref_count,
        "stale_detection_count": stale_detection_count,
        "stale_recovered_count": stale_recovered_count,
        "unresolved_stale_count": unresolved_stale_count,
        "unresolved_stale_rate": round(unresolved_stale_rate, 4),
        "stale_recovery_latency": {
            "samples": stale_recovered_count,
            "p50_ms": stale_latency_p50,
            "p95_ms": stale_latency_p95,
        },
        "latency": latency,
        "top_repeated_commands": [
            {"command": cmd, "count": count}
            for cmd, count in top_repeats
            if count > 1
        ],
    }


def compare_summaries(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare two trace summaries and return delta metrics."""
    def _delta(baseline_val: Any, candidate_val: Any, lower_is_better: bool = True) -> dict[str, Any]:
        if baseline_val is None or candidate_val is None:
            return {"status": "N/A"}
        diff = candidate_val - baseline_val
        pct = (diff / abs(baseline_val) * 100) if baseline_val != 0 else None
        if lower_is_better:
            status = "IMPROVED" if diff < 0 else "REGRESSED" if diff > 0 else "SAME"
        else:
            status = "IMPROVED" if diff > 0 else "REGRESSED" if diff < 0 else "SAME"
        return {
            "baseline": baseline_val,
            "candidate": candidate_val,
            "delta": round(diff, 4),
            "pct_change": round(pct, 2) if pct is not None else None,
            "status": status,
        }

    return {
        "thrash_ratio": _delta(baseline.get("thrash_ratio"), candidate.get("thrash_ratio"), lower_is_better=True),
        "failure_rate": _delta(baseline.get("failure_rate"), candidate.get("failure_rate"), lower_is_better=True),
        "flaky_command_rate": _delta(baseline.get("flaky_command_rate"), candidate.get("flaky_command_rate"), lower_is_better=True),
        "replay_rate": _delta(baseline.get("replay_rate"), candidate.get("replay_rate"), lower_is_better=True),
        "unresolved_stale_rate": _delta(baseline.get("unresolved_stale_rate"), candidate.get("unresolved_stale_rate"), lower_is_better=True),
        "retry_event_count": _delta(baseline.get("retry_event_count"), candidate.get("retry_event_count"), lower_is_better=True),
    }


def format_summary_csv(summary: dict[str, Any]) -> str:
    """Format summary as CSV row with header."""
    # Flatten nested structures for CSV
    row = {
        "event_count": summary.get("event_count"),
        "command_count": summary.get("command_count"),
        "unique_command_count": summary.get("unique_command_count"),
        "repeated_command_count": summary.get("repeated_command_count"),
        "retry_event_count": summary.get("retry_event_count"),
        "failure_event_count": summary.get("failure_event_count"),
        "failure_rate": summary.get("failure_rate"),
        "flaky_command_count": summary.get("flaky_command_count"),
        "flaky_command_rate": summary.get("flaky_command_rate"),
        "thrash_ratio": summary.get("thrash_ratio"),
        "executed_action_count": summary.get("executed_action_count"),
        "replayed_execution_count": summary.get("replayed_execution_count"),
        "replay_rate": summary.get("replay_rate"),
        "missing_side_effect_ref_count": summary.get("missing_side_effect_ref_count"),
        "stale_detection_count": summary.get("stale_detection_count"),
        "stale_recovered_count": summary.get("stale_recovered_count"),
        "unresolved_stale_count": summary.get("unresolved_stale_count"),
        "unresolved_stale_rate": summary.get("unresolved_stale_rate"),
        "stale_recovery_p50_ms": summary.get("stale_recovery_latency", {}).get("p50_ms"),
        "stale_recovery_p95_ms": summary.get("stale_recovery_latency", {}).get("p95_ms"),
        "latency_p50_ms": summary.get("latency", {}).get("p50_ms"),
        "latency_p95_ms": summary.get("latency", {}).get("p95_ms"),
    }

    header = ",".join(row.keys())
    values = ",".join(str(v) for v in row.values())
    return f"{header}\n{values}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Score an agent JSONL trace for thrash/reliability signals")
    parser.add_argument("trace", type=Path, help="Path to JSONL trace file")
    parser.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress output, only return exit code",
    )
    parser.add_argument("--max-thrash", type=float, default=1.0, help="Fail if thrash_ratio exceeds this value")
    parser.add_argument("--max-failure-rate", type=float, default=1.0, help="Fail if failure_rate exceeds this value")
    parser.add_argument("--max-flaky-rate", type=float, default=1.0, help="Fail if flaky_command_rate exceeds this value")
    parser.add_argument("--max-replay-rate", type=float, default=1.0, help="Fail if replay_rate exceeds this value")
    parser.add_argument(
        "--max-unresolved-stale-rate",
        type=float,
        default=1.0,
        help="Fail if unresolved_stale_rate exceeds this value",
    )
    args = parser.parse_args()

    events = load_events(args.trace)
    summary = summarize(events)

    if not args.quiet:
        if args.format == "csv":
            print(format_summary_csv(summary))
        else:
            print(json.dumps(summary, indent=2))

    if summary["thrash_ratio"] > args.max_thrash:
        print(
            f"FAIL: thrash_ratio={summary['thrash_ratio']} exceeds max={args.max_thrash}",
            file=sys.stderr,
        )
        return 2

    if summary["failure_rate"] > args.max_failure_rate:
        print(
            f"FAIL: failure_rate={summary['failure_rate']} exceeds max={args.max_failure_rate}",
            file=sys.stderr,
        )
        return 3

    if summary["flaky_command_rate"] > args.max_flaky_rate:
        print(
            f"FAIL: flaky_command_rate={summary['flaky_command_rate']} exceeds max={args.max_flaky_rate}",
            file=sys.stderr,
        )
        return 6

    if summary["replay_rate"] > args.max_replay_rate:
        print(
            f"FAIL: replay_rate={summary['replay_rate']} exceeds max={args.max_replay_rate}",
            file=sys.stderr,
        )
        return 4

    if summary["unresolved_stale_rate"] > args.max_unresolved_stale_rate:
        print(
            f"FAIL: unresolved_stale_rate={summary['unresolved_stale_rate']} exceeds max={args.max_unresolved_stale_rate}",
            file=sys.stderr,
        )
        return 5

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
