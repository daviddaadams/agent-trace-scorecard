import json
import tempfile
import unittest
from pathlib import Path

import scorecard


class ScorecardTests(unittest.TestCase):
    def test_summarize_repeats_and_retries(self):
        events = [
            {"type": "command_execution", "command": "ls -la", "duration_ms": 120, "success": True},
            {"type": "command_execution", "command": "ls -la", "duration_ms": 180, "success": True},
            {"type": "command_execution", "command": "pytest -q", "duration_ms": 900, "status": "failed"},
            {"type": "retry", "reason": "timeout", "duration_ms": 1500},
            {"type": "tool_call", "arguments": {"command": "pytest -q", "duration_ms": 650}},
            {
                "type": "action.execution.stale_detected",
                "action_id": "act_1",
                "created_at": "2026-03-07T09:00:00+00:00",
            },
            {
                "type": "action.executed",
                "action_id": "act_1",
                "created_at": "2026-03-07T09:00:01+00:00",
                "payload": {"replayed": True, "side_effect_ref": "fx_1"},
            },
            {
                "type": "action.executed",
                "payload": {"replayed": False},
            },
        ]

        s = scorecard.summarize(events)
        self.assertEqual(s["command_count"], 4)
        self.assertEqual(s["unique_command_count"], 2)
        self.assertEqual(s["repeated_command_count"], 2)
        self.assertEqual(s["retry_event_count"], 1)
        self.assertEqual(s["failure_event_count"], 2)
        self.assertAlmostEqual(s["failure_rate"], 0.25)
        self.assertEqual(s["flaky_command_count"], 1)
        self.assertAlmostEqual(s["flaky_command_rate"], 0.5)
        self.assertAlmostEqual(s["thrash_ratio"], 0.5)
        self.assertEqual(s["executed_action_count"], 2)
        self.assertEqual(s["replayed_execution_count"], 1)
        self.assertAlmostEqual(s["replay_rate"], 0.5)
        self.assertEqual(s["missing_side_effect_ref_count"], 1)
        self.assertEqual(s["stale_detection_count"], 1)
        self.assertEqual(s["stale_recovered_count"], 1)
        self.assertEqual(s["unresolved_stale_count"], 0)
        self.assertAlmostEqual(s["unresolved_stale_rate"], 0.0)
        self.assertEqual(s["stale_recovery_latency"]["samples"], 1)
        self.assertEqual(s["stale_recovery_latency"]["p50_ms"], 1000.0)
        self.assertEqual(s["latency"]["samples"], 5)
        self.assertEqual(s["latency"]["p50_ms"], 650.0)
        self.assertEqual(s["latency"]["p95_ms"], 1500.0)

    def test_load_events_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trace.jsonl"
            rows = [
                {"type": "command_execution", "command": "echo hi"},
                {"type": "retry", "reason": "x"},
            ]
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            loaded = scorecard.load_events(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["command"], "echo hi")


if __name__ == "__main__":
    unittest.main()
