"""Black-box full worker contract tests; benchmark parameter repeats are subtests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
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

    def worker(self, language, environment=None):
        row = {"language": language, "dataset": "smoke", "workers": 4, "profile": "controlled"}
        return BatchWorker(suite_command(ROOT, self.data, row), ROOT, environment=environment or self.environment)

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

    def scratch_parent(self, worker, fallback):
        return Path(worker.lease.environment()["BENCH_SCRATCH_ROOT"]) if worker.lease is not None else fallback

    def created_scratch(self, worker, parent, before):
        candidates = [path for path in parent.iterdir() if path not in before and (path / "ownership.json").is_file()]
        self.assertEqual(1, len(candidates))
        path = candidates[0]
        marker = json.loads((path / "ownership.json").read_text())
        self.assertEqual("benchmark-worker-scratch", marker["kind"])
        self.assertEqual(worker.process.pid, marker["pid"])
        self.assertEqual("environment", marker["scratch_root_source"])
        self.assertRegex(marker["token"], r"^[0-9a-f]{32}$")
        self.assertTrue(path.name.endswith(marker["token"]))
        return path

    def test_io_prepare_repairs_same_length_corruption_without_adopting_old_files(self):
        canonical = (self.data / "random-1m.bin").read_bytes()[:65536]
        for language in ("csharp", "java"):
            with self.subTest(language=language), tempfile.TemporaryDirectory(prefix="benchmark-worker-clean-start-") as directory:
                fallback = Path(directory)
                environment = dict(self.environment, BENCH_SCRATCH_ROOT=str(fallback))
                with self.worker(language, environment) as worker:
                    parent = self.scratch_parent(worker, fallback)
                    parent.mkdir(parents=True, exist_ok=True)
                    legacy = self.data / f"worker-csharp-{worker.process.pid}"
                    legacy.mkdir(exist_ok=True)
                    stale = legacy / "io.bin"
                    stale.write_bytes(b"x" * len(canonical))
                    before = set(parent.iterdir())
                    validate_check(worker.check("I01", "read-buffered"), expected_suite(self.data, "I01", "read-buffered"))
                    owned = self.created_scratch(worker, parent, before)
                    self.assertNotEqual(legacy.resolve(), owned.resolve())
                    self.assertEqual(canonical, (owned / "io.bin").read_bytes())
                    self.assertEqual(b"x" * len(canonical), stale.read_bytes())
                    (owned / "io.bin").write_bytes(b"y" * len(canonical))
                    worker.request("PREPARE", "I02", "random-4KiB", 1)
                    self.assertEqual(canonical, (owned / "io.bin").read_bytes())
                    validate_check(worker.request("CHECK", "I02", "random-4KiB"), expected_suite(self.data, "I02", "random-4KiB"))
                    (owned / "io.bin").write_bytes(b"partial")
                    worker.request("PREPARE", "I01", "read-buffered", 1)
                    self.assertEqual(canonical, (owned / "io.bin").read_bytes())
                self.assertFalse(owned.exists())
                self.assertEqual(b"x" * len(canonical), stale.read_bytes())

    def test_abrupt_worker_exit_leaves_no_state_for_the_next_worker_to_adopt(self):
        for language in ("csharp", "java"):
            with self.subTest(language=language), tempfile.TemporaryDirectory(prefix="benchmark-worker-crash-") as directory:
                fallback = Path(directory)
                environment = dict(self.environment, BENCH_SCRATCH_ROOT=str(fallback))
                first = self.worker(language, environment)
                try:
                    parent = self.scratch_parent(first, fallback)
                    parent.mkdir(parents=True, exist_ok=True)
                    before = set(parent.iterdir())
                    first.check("I01", "read-buffered")
                    abandoned = self.created_scratch(first, parent, before)
                    first.process.kill()
                    first.process.wait(timeout=10)
                finally:
                    first.close()
                (abandoned / "io.bin").write_bytes(b"stale-after-crash" * 4096)
                stale_bytes = (abandoned / "io.bin").read_bytes()
                with self.worker(language, environment) as following:
                    before = set(parent.iterdir())
                    validate_check(following.check("I01", "read-buffered"), expected_suite(self.data, "I01", "read-buffered"))
                    fresh = self.created_scratch(following, parent, before)
                    self.assertNotEqual(abandoned, fresh)
                    self.assertEqual(stale_bytes, (abandoned / "io.bin").read_bytes())
                self.assertFalse(fresh.exists())
                self.assertEqual(stale_bytes, (abandoned / "io.bin").read_bytes())

    def test_cleanup_obstruction_is_reported_without_deleting_unknown_files(self):
        for language in ("csharp", "java"):
            with self.subTest(language=language), tempfile.TemporaryDirectory(prefix="benchmark-worker-close-") as directory:
                fallback = Path(directory)
                environment = dict(self.environment, BENCH_SCRATCH_ROOT=str(fallback))
                worker = self.worker(language, environment)
                try:
                    parent = self.scratch_parent(worker, fallback)
                    parent.mkdir(parents=True, exist_ok=True)
                    before = set(parent.iterdir())
                    worker.check("A03", "atomic")
                    worker.check("I01", "read-buffered")
                    owned = self.created_scratch(worker, parent, before)
                    (owned / "io.bin").unlink()
                    (owned / "io.bin").mkdir()
                    sentinel = owned / "io.bin" / "preserve.txt"
                    sentinel.write_text("unknown file must survive nonrecursive cleanup")
                finally:
                    worker.close()
                self.assertEqual(0, worker.process.returncode)
                self.assertEqual("unknown file must survive nonrecursive cleanup", sentinel.read_text())
                self.assertTrue((owned / "ownership.json").is_file())
                deadline = time.monotonic() + 2
                while "cleanup_incomplete" not in "".join(worker.stderr) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIn("cleanup_incomplete", "".join(worker.stderr))


if __name__ == "__main__":
    unittest.main()
