"""Process scheduling, output-consumption and common collector contract tests."""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller.execution import BatchWorker, ProcessSampler, paired_schedule


def rows(repetitions=5):
    return [{"case_id": "C01", "parameter_id": "transform", "engine": None, "access": None,
             "profile": "controlled", "dataset": "S", "workers": 1, "repetition": repetition,
             "track": "lts", "language": language}
            for repetition in range(1, repetitions + 1) for language in ("csharp", "java")]


class ExecutionTests(unittest.TestCase):
    def test_seeded_pair_order_is_balanced_and_deterministic(self):
        schedule = paired_schedule(rows())
        self.assertEqual(schedule, paired_schedule(rows()))
        first_csharp = sum(pair["order"][0] == "csharp" for pair in schedule)
        self.assertLessEqual(abs(first_csharp - (len(schedule) - first_csharp)), 1)
        self.assertTrue(all({row["language"] for row in pair["rows"]} == {"csharp", "java"} for pair in schedule))

    def test_incomplete_language_pair_rejected(self):
        with self.assertRaisesRegex(ValueError, "complete paired"):
            paired_schedule(rows()[:-1])

    def test_duplicate_language_pair_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            paired_schedule(rows() + [rows()[0]])

    def test_common_os_sampler_reads_only_requested_pid(self):
        sampler = ProcessSampler(os.getpid())
        try:
            first = sampler.snapshot()
            self.assertGreater(first["rss_bytes"], 0)
            self.assertGreaterEqual(first["cpu_seconds"], 0)
            self.assertGreater(sampler.snapshot()["monotonic_seconds"], first["monotonic_seconds"])
        finally:
            sampler.close()

    def batch_worker(self, output):
        worker = BatchWorker.__new__(BatchWorker)
        worker.request = lambda *_: output
        return worker

    def test_batch_must_finish_frozen_iteration_count(self):
        worker = self.batch_worker({"iterations": 1, "checksum": "0" * 16, "work_units": 1, "unit": "items"})
        with self.assertRaisesRegex(ValueError, "fewer"):
            worker.batch("C01", "transform", 2, collect=False)

    def test_batch_output_must_be_consumed_and_match_oracle(self):
        worker = self.batch_worker({"iterations": 2, "checksum": "0" * 16, "work_units": 2, "unit": "items"})
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            worker.batch("C01", "transform", 2, "0000000000000001", collect=False)

    def test_batch_uses_unsigned_modulo_checksum_aggregation(self):
        worker = self.batch_worker({"iterations": 2, "checksum": "fffffffffffffffe", "work_units": 2, "unit": "items"})
        result = worker.batch("C01", "transform", 2, "ffffffffffffffff", collect=False)
        self.assertEqual(2, result["successful_work_units"])


if __name__ == "__main__":
    unittest.main()
