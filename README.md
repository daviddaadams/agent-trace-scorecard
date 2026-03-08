# agent-trace-scorecard

Tiny OSS-friendly CLI to score agent JSONL traces for reliability regressions.

## Why
Agent systems silently regress when they start looping/retrying/thrashing.
This tool turns raw traces into guardrail metrics you can gate in CI.

## Metrics
- `command_count`
- `unique_command_count`
- `repeated_command_count`
- `retry_event_count`
- `failure_event_count`
- `failure_rate`
- `flaky_command_count` (commands seen with both success + failure outcomes)
- `flaky_command_rate` (`flaky_command_count / unique_command_count`)
- `latency.p50_ms` / `latency.p95_ms`
- `thrash_ratio` (repeated / total commands)
- `executed_action_count` / `replayed_execution_count` / `replay_rate`
- `missing_side_effect_ref_count` (executed events without side-effect lineage)
- `stale_detection_count` / `stale_recovered_count` / `unresolved_stale_rate`
- `stale_recovery_latency.p50_ms` / `stale_recovery_latency.p95_ms`

## Usage

```bash
python3 scorecard.py path/to/trace.jsonl --max-thrash 0.35 --max-failure-rate 0.20 --max-flaky-rate 0.30 --max-replay-rate 0.30 --max-unresolved-stale-rate 0.20
```

Returns exit code:
- `2` when thrash exceeds threshold
- `3` when failure rate exceeds threshold
- `4` when replay rate exceeds threshold
- `5` when unresolved stale-execution rate exceeds threshold
- `6` when flaky command rate exceeds threshold

## Test

```bash
python3 -m unittest -v test_scorecard.py
```

## License
MIT (add `LICENSE` before publishing).
