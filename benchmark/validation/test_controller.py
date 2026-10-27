"""Controller failure paths independent of installed SDKs and database services."""

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import benchmarkctl as ctl


class ControllerTests(unittest.TestCase):
    def config(self):
        return ctl.read_json(ctl.ROOT / "config" / "campaign.json")

    def reject_config(self, **changes):
        config = self.config()
        config.update(changes)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "campaign.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises((ValueError, ctl.Blocked)):
                ctl.resolve_config(path)

    def test_rejects_negative_nominal_duration(self):
        self.reject_config(measurement_seconds=-120)

    def test_rejects_below_contract_warmup(self):
        self.reject_config(warmup_seconds=10)

    def test_rejects_invalid_mutation_trace_size(self):
        self.reject_config(mutation_operations=0)

    def test_rejects_negative_worker_budget(self):
        self.reject_config(network_workers=[1, -8, 32])

    def test_rejects_hidden_sqlite_contention_profile(self):
        self.reject_config(sqlite_workers=[8])

    def test_rejects_silently_removed_engine(self):
        self.reject_config(engines=["sqlite"])

    def test_rejects_unselected_runtime_track(self):
        self.reject_config(track="requested")

    def test_selectors_fail_when_no_rows_match(self):
        with self.assertRaises(ValueError):
            ctl.inventory(ctl.ROOT, self.config(), {"case": "C01", "engine": "sqlite"})

    def test_unknown_authored_case_fails(self):
        with self.assertRaises(ValueError):
            ctl.inventory(ctl.ROOT, self.config(), {"case": "C99"})

    def test_atomic_write_replaces_complete_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "checkpoint.json"
            ctl.atomic_json(target, {"first": True})
            ctl.atomic_json(target, {"second": True})
            self.assertEqual({"second": True}, ctl.read_json(target))
            self.assertEqual([target], list(Path(temporary).iterdir()))

    def test_dry_run_preserves_exact_dal_count_and_performs_no_processes(self):
        with patch.object(ctl, "run_command", side_effect=AssertionError("Process launched during dry run")):
            result = ctl.dry_run(self.config(), {"case": "D01,D02,D03,D04,D05,D06,D07,D08"}, "full")
        self.assertFalse(result["executed"])
        self.assertEqual(1120, result["generated_inventory_rows"])
        self.assertEqual(172800, result["estimated_wall_seconds"]["network_dal_nominal"])
        self.assertEqual(28800, result["estimated_wall_seconds"]["sqlite_dal_nominal"])

    def test_worker_runtime_is_checked_inside_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "one.i64le"
            payload.write_bytes(struct.pack("<q", 1))
            ready = {"status": "ready", "language": "csharp", "track": "lts", "protocol": 1,
                     "measured": False, "runtime": ".NET 9.0.0", "items": 1}
            response = ctl.expected([1], "C01", "transform")
            raw = {"command": [], "cwd": str(ctl.ROOT), "exit_code": 0,
                   "stdout": json.dumps(ready) + "\n" + json.dumps(response), "stderr": ""}
            with patch.object(ctl, "run_command", return_value=raw):
                with self.assertRaisesRegex(ValueError, "runtime"):
                    ctl.check_worker("csharp", payload, [("C01", "transform")], 60)

    def test_preflight_failures_do_not_connect_database(self):
        with patch.object(ctl, "run_command", side_effect=ctl.Blocked("missing")), patch.object(ctl, "file_hash", return_value="a" * 64):
            result = ctl.preflight(self.config())
        self.assertEqual("blocked", result["status"])
        self.assertFalse(result["database_connections_attempted"])


if __name__ == "__main__":
    unittest.main()
