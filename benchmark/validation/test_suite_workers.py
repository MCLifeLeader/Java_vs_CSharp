"""Black-box full worker contract tests; benchmark parameter repeats are subtests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.execution import BatchWorker
from controller.services import LocalServices
from controller.suite import suite_command, validate_check, worker_environment
from data.generate_suite import generate_suite
from validation.suite_oracle import expected_suite


class SuiteWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        required = [ROOT / "dotnet/Suite/bin/Release/net10.0/Suite.dll", ROOT / "java/suite/target/suite.jar"]
        for path in required:
            if not path.is_file():
                raise RuntimeError(f"Build suite before verification: {path}")
        cls.environment = worker_environment(ROOT)
        native = cls.environment.get("BENCH_NATIVE_LIBRARY")
        if not native or not Path(native).is_file():
            raise RuntimeError("Build the common native library using benchmark/native/build.py")
        cls.temporary = tempfile.TemporaryDirectory(prefix="benchmark-suite-test-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.data = Path(cls.temporary.name)
        generate_suite(cls.data, "smoke")
        cls.services = LocalServices(ROOT, cls.data)
        cls.services.__enter__()
        cls.addClassCleanup(cls.services.__exit__, None, None, None)
        catalog = json.loads((ROOT / "config/cases.json").read_text(encoding="utf-8"))
        cls.parameters = [(case["case_id"], parameter["parameter_id"]) for case in catalog["cases"]
                          if case["case_id"][0] not in "DHP" and case["case_id"] != "L05"
                          for parameter in case["parameters"]]

    def worker(self, language):
        row = {"language": language, "dataset": "smoke", "workers": 4, "profile": "controlled"}
        return BatchWorker(suite_command(ROOT, self.data, row), ROOT, environment=self.environment)

    def test_every_parameter_check_and_repeated_batch_match_independent_oracle(self):
        self.assertEqual(94, len(self.parameters))
        for language in ("csharp", "java"):
            with self.worker(language) as worker:
                for case_id, parameter in self.parameters:
                    with self.subTest(language=language, case_id=case_id, parameter=parameter):
                        expected = expected_suite(self.data, case_id, parameter, workers=4)
                        check = worker.check(case_id, parameter)
                        validate_check(check, expected)
                        batch = worker.batch(case_id, parameter, 2, expected["checksum"], collect=False)
                        self.assertEqual(expected["work_units"] * 2, batch["successful_work_units"])
                        self.assertEqual(expected["unit"], batch["unit"])

    def test_unknown_case_parameter_and_command_are_rejected_without_losing_protocol(self):
        for language in ("csharp", "java"):
            with self.worker(language) as worker:
                for command, case_id, parameter in (("CHECK", "X99", "unknown"), ("CHECK", "C01", "unknown"), ("INVALID", "C01", "transform")):
                    with self.subTest(language=language, command=command, case_id=case_id):
                        with self.assertRaises(ValueError):
                            worker.request(command, case_id, parameter)
                validate_check(worker.check("C01", "transform"), expected_suite(self.data, "C01", "transform", workers=4))

    def test_sort_batches_preserve_original_input_for_following_operations(self):
        for language in ("csharp", "java"):
            with self.subTest(language=language), self.worker(language) as worker:
                before = worker.check("C01", "transform")
                oracle = expected_suite(self.data, "K01", "sort", workers=4)
                worker.batch("K01", "sort", 3, oracle["checksum"], collect=False)
                after = worker.check("C01", "transform")
                self.assertEqual(before["semantic_hash"], after["semantic_hash"])
                validate_check(worker.check("K01", "sort"), oracle)


if __name__ == "__main__":
    unittest.main()
