"""Failure gates for correctness-only result admission and checkpoint integrity."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reporting.report import canonical_hash, summarize, validate_result, verify_evidence


def signed_record():
    record = {
        "contract_version": "smoke-1", "campaign_id": "test", "case_id": "C01", "parameter_id": "transform",
        "repetition": 1, "language": "csharp", "engine": None, "access": None,
        "profile": "controlled", "dataset": "smoke", "workers": 1, "track": "lts",
        "case_hash": "a" * 64, "status": "validated", "fingerprint": "b" * 64,
        "provenance": {"utc_start": "2026-10-27T12:00:00+00:00", "utc_end": "2026-10-27T12:00:01+00:00",
                       "source_commit": "a" * 40, "source_hash": "c" * 64, "dirty_source": True,
                       "command": ["dotnet", "worker.dll"], "host_manifest_hash": "d" * 64,
                       "versions_hash": "e" * 64, "config_hash": "f" * 64},
        "input": {"dataset_hash": "1" * 64, "seed": 20261025,
                  "encoding": "signed-int64-little-endian", "payload_bytes": 8},
        "outcome": {"attempts": 1, "successes": 1, "failures": 0, "correctness": "pass",
                    "checksum": "0000000000000000", "items": 1},
        "performance": {"measured": False, "throughput_ops_s": None, "cpu_seconds": None,
                        "peak_rss_bytes": None, "p99_ms": None, "unavailable_reason": "Smoke only"},
        "admission": {"admitted": False, "reason": "No campaign"}, "evidence": {"evidence/test/log.json": "2" * 64},
    }
    identity = {key: record[key] for key in ("case_id", "parameter_id", "engine", "access", "profile", "dataset",
                                            "workers", "repetition", "track")}
    record["pair_id"] = canonical_hash(identity)
    return resign(record)


def resign(record):
    record["result_hash"] = canonical_hash({key: value for key, value in record.items() if key != "result_hash"})
    return record


class ReportingTests(unittest.TestCase):
    def test_valid_smoke_is_accepted_without_scoring(self):
        validate_result(signed_record())

    def test_changed_content_rejects_original_hash(self):
        record = signed_record()
        record["outcome"]["checksum"] = "1" * 16
        with self.assertRaisesRegex(ValueError, "content hash"):
            validate_result(record)

    def test_success_count_cannot_exceed_attempts_even_when_rehashed(self):
        record = signed_record()
        record["outcome"]["successes"] = 2
        with self.assertRaises(ValueError):
            validate_result(resign(record))

    def test_smoke_cannot_be_admitted_even_when_rehashed(self):
        record = signed_record()
        record["admission"]["admitted"] = True
        with self.assertRaises(ValueError):
            validate_result(resign(record))

    def test_smoke_cannot_gain_fabricated_measurement(self):
        record = signed_record()
        record["performance"]["throughput_ops_s"] = 100
        with self.assertRaises(ValueError):
            validate_result(resign(record))

    def test_negative_chronology_is_rejected(self):
        record = signed_record()
        record["provenance"]["utc_end"] = "2026-10-26T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "chronology"):
            validate_result(resign(record))

    def test_byte_count_must_match_returned_items(self):
        record = signed_record()
        record["input"]["payload_bytes"] = 16
        with self.assertRaisesRegex(ValueError, "item count"):
            validate_result(resign(record))

    def test_pair_identity_cannot_be_changed(self):
        record = signed_record()
        record["repetition"] = 2
        with self.assertRaisesRegex(ValueError, "Paired identity"):
            validate_result(resign(record))

    def test_schema_rejects_unknown_fields(self):
        record = signed_record()
        record["coverage_percentage"] = 100
        with self.assertRaisesRegex(ValueError, "unknown field"):
            validate_result(resign(record))

    def test_evidence_path_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = signed_record()
            record["evidence"] = {"../outside.json": "2" * 64}
            with self.assertRaisesRegex(ValueError, "outside"):
                verify_evidence(temporary, record)

    def test_modified_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "evidence" / "test" / "log.json"
            path.parent.mkdir(parents=True)
            path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                verify_evidence(root, signed_record())

    def test_manifest_removed_gap_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "results" / "test"
            evidence_path = root / "evidence" / "test"
            result_path.mkdir(parents=True)
            evidence_path.mkdir(parents=True)
            manifest = {"campaign_id": "test", "fingerprint": "b" * 64, "config": {"track": "lts"},
                        "selected_inventory": [], "gaps": [{"implemented": False}]}
            manifest["manifest_hash"] = canonical_hash(manifest)
            (evidence_path / "campaign-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            changed = deepcopy(manifest)
            changed["gaps"] = []
            changed["manifest_hash"] = canonical_hash({key: value for key, value in changed.items() if key != "manifest_hash"})
            (result_path / "manifest.json").write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "original evidence"):
                summarize(root, "test")


if __name__ == "__main__":
    unittest.main()
