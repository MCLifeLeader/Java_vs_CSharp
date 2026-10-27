"""Cross-language database semantics against an independent dictionary oracle.

Build database modules and generate suite fixtures before running. No measurements
are admitted by these tests; native-engine parity remains a separate gate.
"""
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
import os
import shutil
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".work/suite-data/smoke"
DOTNET = ROOT / "dotnet/Database/bin/Release/net10.0/Database.dll"
JAVA = ROOT / "java/database/target/database-worker-1.0.0-all.jar"
sys.path.insert(0, str(ROOT))


from database_oracle import Oracle, new_order


def run_worker(command, *, input=None, timeout=60, env=None, text=True, capture_output=True):
    """Preserve CompletedProcess results while inheriting an active verification lease."""
    from controller.benchmarkctl import run_command
    if not text or not capture_output:
        raise ValueError("Worker contracts require captured text results")
    record = run_command(command, cwd=ROOT, timeout=timeout, stdin=input, environment=env)
    return subprocess.CompletedProcess(command, record["exit_code"], record["stdout"], record["stderr"])


@unittest.skipUnless(DOTNET.exists() and JAVA.exists(), "Build both DB modules first")
class DatabaseWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global DATA
        from data.generate_suite import generate_suite
        previous = DATA
        temporary = tempfile.TemporaryDirectory(prefix="db-worker-fixtures-")
        cls.addClassCleanup(temporary.cleanup)
        cls.addClassCleanup(lambda: globals().__setitem__("DATA", previous))
        DATA = Path(temporary.name)
        generate_suite(DATA, "smoke")

    def test_failed_reset_and_abrupt_restart_restore_verified_canonical_state(self):
        initial = Oracle(DATA).state()
        for language in ("csharp", "java"):
            for access in ("raw", "orm"):
                with self.subTest(language=language, access=access), tempfile.TemporaryDirectory(prefix="db-reset-") as temporary:
                    fixture = Path(temporary)
                    for name in ("customers.csv", "products.csv", "orders.csv", "order_lines.csv"):
                        shutil.copyfile(DATA / name, fixture / name)
                    command = (["dotnet", str(DOTNET)] if language == "csharp" else
                               ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(JAVA)])
                    command += [str(ROOT), "sqlite", access, str(fixture)]
                    def exchange(process, request):
                        process.stdin.write(json.dumps(request) + "\n")
                        process.stdin.flush()
                        return json.loads(process.stdout.readline())
                    with (fixture / "stderr.txt").open("w", encoding="utf-8") as errors:
                        from controller.clean_start import current_run
                        lease = current_run()
                        environment = {**os.environ, **lease.environment()} if lease else None
                        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                                   text=True, env=environment, start_new_session=os.name != "nt")
                        if lease:
                            lease.track_process(process)
                        try:
                            self.assertEqual(initial, json.loads(process.stdout.readline())["state"])
                            oracle = Oracle(DATA)
                            for case, parameter in [("D05", {"customer_id": 1, "delta_cents": 21}), ("D06", new_order(31000))]:
                                self.assertEqual(oracle.execute(case, parameter), exchange(process, {"case_id": case, "input": parameter})["result"])
                            dirty = oracle.state()
                            self.assertNotEqual(initial, dirty)
                            self.assertEqual("error", exchange(process, {"case_id": "RESET", "inject_failure": True})["status"])
                            self.assertEqual(dirty, exchange(process, {"case_id": "STATE"})["result"])
                            self.assertEqual("error", exchange(process, {"case_id": "D01", "input": {"customer_id": 1}})["status"])
                            product_file = fixture / "products.csv"
                            original = product_file.read_bytes()
                            product_file.write_bytes(original.replace(b"Product", b"Changed", 1))
                            self.assertNotEqual(original, product_file.read_bytes())
                            self.assertEqual("error", exchange(process, {"case_id": "RESET"})["status"])
                            self.assertEqual(dirty, exchange(process, {"case_id": "STATE"})["result"])
                            product_file.write_bytes(original)
                            self.assertEqual(initial, exchange(process, {"case_id": "RESET"})["result"])
                            self.assertEqual(Oracle(DATA).execute("D01", {"customer_id": 1}), exchange(process, {"case_id": "D01", "input": {"customer_id": 1}})["result"])
                            self.assertEqual("ok", exchange(process, {"case_id": "D06", "input": new_order(31000)})["status"])
                            self.assertEqual("error", exchange(process, {"case_id": "H06", "input": new_order(31001, inject_failure=True)})["status"])
                            # Terminate without QUIT/disposal after committed and failed work.
                            process.kill()
                            process.wait(timeout=10)
                        finally:
                            if process.poll() is None:
                                process.kill()
                                process.wait(timeout=10)
                            process.stdin.close()
                            process.stdout.close()
                            if lease:
                                lease.process_finished(process)
                    restarted = run_worker(command, input=json.dumps({"case_id": "D06", "input": new_order(31000)}) + '\n{"case_id":"QUIT"}\n', text=True, capture_output=True, timeout=60)
                    self.assertEqual(0, restarted.returncode, restarted.stderr[-2000:])
                    output = [json.loads(line) for line in restarted.stdout.splitlines()]
                    self.assertEqual(initial, output[0]["state"])
                    self.assertEqual("ok", output[1]["status"])

    def test_failed_initial_import_does_not_poison_next_clean_start(self):
        initial = Oracle(DATA).state()
        for language in ("csharp", "java"):
            for access in ("raw", "orm"):
                with self.subTest(language=language, access=access), tempfile.TemporaryDirectory(prefix="db-import-") as temporary:
                    fixture = Path(temporary)
                    for name in ("customers.csv", "products.csv", "orders.csv", "order_lines.csv"):
                        shutil.copyfile(DATA / name, fixture / name)
                    lines = fixture / "order_lines.csv"
                    with lines.open(encoding="utf-8", newline="") as source:
                        rows = list(csv.DictReader(source))
                    rows[-1]["product_id"] = "999999"
                    with lines.open("w", encoding="utf-8", newline="") as target:
                        writer = csv.DictWriter(target, fieldnames=rows[0].keys())
                        writer.writeheader()
                        writer.writerows(rows)
                    command = (["dotnet", str(DOTNET)] if language == "csharp" else
                               ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(JAVA)])
                    command += [str(ROOT), "sqlite", access, str(fixture)]
                    failed = run_worker(command, text=True, capture_output=True, timeout=60)
                    self.assertEqual(1, failed.returncode)
                    self.assertEqual("blocked", json.loads(failed.stdout.splitlines()[-1])["status"])
                    shutil.copyfile(DATA / "order_lines.csv", lines)
                    recovered = run_worker(command, input='{"case_id":"QUIT"}\n', text=True, capture_output=True, timeout=60)
                    self.assertEqual(0, recovered.returncode, recovered.stderr[-2000:])
                    self.assertEqual(initial, json.loads(recovered.stdout.splitlines()[0])["state"])

    def test_untimed_audit_and_commit_reconciliation_preserve_order_and_state(self):
        first_trace = [
            {"case_id": "D01", "input": {"customer_id": 1}},
            {"case_id": "D02", "input": {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}},
            {"case_id": "D03", "input": {"order_id": 1}},
            {"case_id": "D04", "input": {"start_epoch_ms": 0, "end_epoch_ms": 2000000000000}},
            {"case_id": "D05", "input": {"customer_id": 1, "delta_cents": 17}},
            {"case_id": "D06", "input": new_order(10000)},
        ]
        second_trace = [
            {"case_id": "D03", "input": {"order_id": 10000}},
            {"case_id": "H06", "input": new_order(10001)},
            {"case_id": "D07", "input": {"order_id": 2}},
        ]
        oracle = Oracle(DATA)
        initial = oracle.state()
        expected_first = [{"case_id": op["case_id"], "result": oracle.execute(op["case_id"], op["input"])} for op in first_trace]
        expected_second = [{"case_id": op["case_id"], "result": oracle.execute(op["case_id"], op["input"])} for op in second_trace]
        final = oracle.state()
        ids = list(range(601, 0, -1)) + [10000, 10001, 10001]
        committed = sorted(set(ids) & oracle.tables["orders"].keys())
        requests = [
            {"case_id": "AUDIT", "workload": "D08", "trace": first_trace},
            {"case_id": "AUDIT", "workload": "D08", "trace": second_trace},
            {"case_id": "COMMITTED", "ids": ids},
            {"case_id": "COMMITTED", "ids": []},
            {"case_id": "STATE"},
            {"case_id": "AUDIT", "workload": "D06", "trace": [new_order(20000)] * 1001},
            {"case_id": "AUDIT", "workload": "D08", "trace": [
                {"case_id": "D06", "input": new_order(20000)}, {"case_id": "invalid", "input": {}}]},
            {"case_id": "COMMITTED", "ids": [0, 1]},
            {"case_id": "STATE"}, {"case_id": "RESET"},
            {"case_id": "COMMITTED", "ids": [10001, 2, 10000, 1]}, {"case_id": "STATE"},
            {"case_id": "QUIT"},
        ]
        for language in ("csharp", "java"):
            for access in ("raw", "orm"):
                with self.subTest(language=language, access=access):
                    command = (["dotnet", str(DOTNET)] if language == "csharp" else
                               ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(JAVA)])
                    run = run_worker(command + [str(ROOT), "sqlite", access, str(DATA)],
                        input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True, timeout=90)
                    self.assertEqual(0, run.returncode, run.stderr[-2000:])
                    output = [json.loads(line) for line in run.stdout.splitlines()][1:]
                    self.assertEqual({"operations": len(first_trace), "results": expected_first}, output[0]["result"])
                    self.assertEqual({"operations": len(second_trace), "results": expected_second}, output[1]["result"])
                    self.assertEqual(committed, output[2]["result"])
                    self.assertEqual([], output[3]["result"])
                    self.assertEqual(final, output[4]["result"])
                    self.assertTrue(all(row["status"] == "error" for row in output[5:8]), output[5:8])
                    self.assertEqual(final, output[8]["result"])
                    self.assertEqual(initial, output[9]["result"])
                    self.assertEqual([1, 2], output[10]["result"])
                    self.assertEqual(initial, output[11]["result"])

    def test_invalid_targets_and_finite_trace_requests_fail_closed(self):
        initial = Oracle(DATA).state()
        for language in ("csharp", "java"):
            command = (["dotnet", str(DOTNET)] if language == "csharp" else
                       ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(JAVA)])
            clean_environment = {k: v for k, v in os.environ.items() if not k.startswith("BENCH_DB_")}
            denied = run_worker(command + [str(ROOT), "postgresql", "raw", str(DATA)],
                                    text=True, capture_output=True, timeout=30, env=clean_environment)
            with self.subTest(language=language, target="unconfigured network"):
                self.assertEqual(1, denied.returncode)
                self.assertEqual("blocked", json.loads(denied.stdout.splitlines()[-1])["status"])
            owned = "bench_" + "a" * 32
            lease_environment = dict(clean_environment, BENCH_DB_CONNECTION=(
                f"Host=127.0.0.1;Port=1;Database={owned};Username=unused;Password=unused" if language == "csharp" else
                f"jdbc:postgresql://127.0.0.1:1/{owned}"), BENCH_DB_ALLOWED_HOSTS="127.0.0.1",
                BENCH_DB_OWNED_NAME=owned, BENCH_DB_INITIALIZE="1")
            unleased = run_worker(command + [str(ROOT), "postgresql", "raw", str(DATA)],
                                     text=True, capture_output=True, timeout=10, env=lease_environment)
            with self.subTest(language=language, target="allowlisted target without controller lease"):
                self.assertEqual(1, unleased.returncode)
                self.assertEqual("blocked", json.loads(unleased.stdout.splitlines()[-1])["status"])
            invalid = [
                {"case_id": "BATCH", "workload": "D01", "trace": [{"customer_id": 1}], "iterations": 1, "workers": 8},
                {"case_id": "BATCH", "workload": "D06", "trace": [new_order(90000)], "iterations": 2},
                {"case_id": "BATCH", "workload": "D06", "trace": [new_order(90000), new_order(90000)], "iterations": 1},
                {"case_id": "BATCH", "workload": "D08", "trace": [{"case_id": "D01", "input": {"customer_id": 1}}], "iterations": 1},
            ]
            result = run_worker(command + [str(ROOT), "sqlite", "raw", str(DATA)],
                input="".join(json.dumps(r) + "\n" for r in invalid + [{"case_id": "STATE"}, {"case_id": "QUIT"}]),
                text=True, capture_output=True, timeout=60, env=clean_environment)
            with self.subTest(language=language, target="invalid trace"):
                self.assertEqual(0, result.returncode)
                output = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual("ready", output[0]["status"])
                self.assertTrue(all(row["status"] == "error" for row in output[1:-1]), output)
                self.assertEqual(initial, output[-1]["result"])

    def test_four_sqlite_cells_match_oracle_all_workloads_and_rollback(self):
        operations = [
            ("D01", {"customer_id": 1}),
            ("D02", {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}),
            ("D03", {"order_id": 1}),
            ("D04", {"start_epoch_ms": 0, "end_epoch_ms": 2000000000000}),
            ("D05", {"customer_id": 1, "delta_cents": 125}),
            ("D05", {"customer_id": 1, "delta_cents": 125, "expected_version": 0}),
            ("D06", new_order(10000)), ("D03", {"order_id": 10000}),
            ("D06", new_order(10000)), ("D06", new_order(10001, inject_failure=True)),
            ("D07", {"order_id": 2, "expected_version": 999}), ("D07", {"order_id": 2}),
            ("H06", new_order(10002)), ("H06", new_order(10003, inject_failure=True)),
        ]
        mixed = ([{"case_id": "D01", "input": {"customer_id": 1}} for _ in range(12)] +
                 [{"case_id": "D02", "input": {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}} for _ in range(3)] +
                 [{"case_id": "D05", "input": {"customer_id": 2, "delta_cents": 1}} for _ in range(2)] +
                 [{"case_id": "D06", "input": new_order(11000 + i)} for i in range(2)] +
                 [{"case_id": "D07", "input": {"order_id": 3}}])
        mixed[16]["input"]["expected_version"] = 999
        operations.append(("D08", {"trace": mixed}))
        for language in ("csharp", "java"):
            for access in ("raw", "orm"):
                with self.subTest(language=language, access=access):
                    oracle = Oracle(DATA)
                    initial = oracle.state()
                    expected = []
                    for case, p in operations:
                        try:
                            expected.append({"status": "ok", "case_id": case, "result": oracle.execute(case, p)})
                        except ValueError:
                            expected.append({"status": "error"})
                    final = oracle.state()
                    batch = {"case_id": "BATCH", "workload": "D01", "trace": [
                        {"customer_id": 1}, {"customer_id": 2}], "iterations": 2}
                    requests = [{"case_id": case, "input": p} for case, p in operations] + [
                        {"case_id": "STATE"}, batch, {"case_id": "RESET"}, {"case_id": "STATE"}, {"case_id": "QUIT"}]
                    command = (["dotnet", str(DOTNET)] if language == "csharp" else
                               ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(JAVA)])
                    result = run_worker(command + [str(ROOT), "sqlite", access, str(DATA)],
                        input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True, timeout=90)
                    self.assertEqual(0, result.returncode, result.stderr[-2000:])
                    output = [json.loads(line) for line in result.stdout.splitlines()]
                    self.assertEqual("ready", output[0]["status"], result.stderr[-2000:])
                    self.assertEqual(initial, output[0]["state"])
                    for index, (actual, wanted) in enumerate(zip(output[1:], expected)):
                        self.assertEqual(wanted["status"], actual["status"], (index, actual))
                        if wanted["status"] == "ok":
                            self.assertEqual(wanted["result"], actual["result"], (index, operations[index][0]))
                    self.assertEqual(final, output[len(expected) + 1]["result"])
                    measured = output[len(expected) + 2]["result"]
                    self.assertEqual(4, measured["attempts"])
                    self.assertEqual(4, measured["successes"])
                    self.assertEqual({}, measured["categorized_failures"])
                    self.assertEqual(4, len(measured["latencies_ns"]))
                    self.assertTrue(all(t > 0 for t in measured["latencies_ns"]))
                    self.assertFalse(measured["admitted"])
                    self.assertEqual(initial, output[-2]["result"])
                    self.assertEqual(initial, output[-1]["result"])
                    self.assertIn("sqlite_source_id", output[0]["metadata"])
                    self.assertTrue(output[0]["metadata"]["compile_options"])


if __name__ == "__main__":
    unittest.main()
