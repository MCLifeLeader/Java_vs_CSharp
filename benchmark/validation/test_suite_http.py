"""Real framework HTTP contracts. Three authored methods; language/access variants are subtests."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.suite import suite_command, worker_environment
from controller.toolchain import activate_toolchain
from data.generate_suite import generate_suite
from validation.database_oracle import Oracle
from validation.suite_oracle import expected_suite


class SuiteHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_toolchain(ROOT)
        cls.temporary = tempfile.TemporaryDirectory(prefix="benchmark-http-contract-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.directory = Path(cls.temporary.name)
        cls.data = cls.directory / "data"
        generate_suite(cls.data, "smoke")
        cls.environment = worker_environment(ROOT)
        cls.artifacts = {}
        # Owned copies keep server lifetimes from locking shared build outputs.
        sources = {"java": Path(os.environ.get("BENCH_JAVA_SUITE_JAR", ROOT / "java/suite/target/suite.jar")),
                   "csharp": ROOT / "dotnet/Suite/bin/Release/net10.0/Suite.dll"}
        for language, source in sources.items():
            if not source.is_file():
                raise RuntimeError(f"Build full suite before HTTP verification: {source}")
            destination = cls.directory / language
            if language == "csharp":
                shutil.copytree(source.parent, destination)
            else:
                destination.mkdir()
                shutil.copy2(source, destination / source.name)
            canonical = ROOT / "java/suite/target/suite.jar" if language == "java" else source
            cls.artifacts[language] = (str(canonical), str(destination / source.name))
        cls.requests = []

    @classmethod
    def tearDownClass(cls):
        destination = ROOT / ".work/test-suite-http-evidence.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({"framework": "unittest", "authored_methods": 3,
            "execution_kind": "functional HTTP assertions; no performance measurement",
            "executed_requests": len(cls.requests), "requests": cls.requests}, indent=2))

    @contextmanager
    def server(self, language, access=None):
        row = {"language": language, "dataset": "smoke", "workers": 1,
               "profile": "controlled", "engine": "sqlite" if access else None,
               "access": access}
        command = suite_command(ROOT, self.data, row, server=True)
        original, copied = self.artifacts[language]
        command = [copied if value == original else value for value in command]
        from controller.clean_start import current_run
        lease = current_run(ROOT)
        environment = {**self.environment, **(lease.environment() if lease is not None else {})}
        stdout = queue.Queue()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as diagnostics:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=diagnostics, text=True, encoding="utf-8",
                env=environment, start_new_session=os.name != "nt")
            def read():
                for line in process.stdout:
                    stdout.put(line)
                stdout.put(None)
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                if lease is not None:
                    lease.track_process(process)
                line = stdout.get(timeout=60)
                self.assertIsNotNone(line, "HTTP worker exited before ready")
                ready = json.loads(line)
                self.assertEqual("ready", ready.get("status"))
                self.assertEqual(2, ready.get("protocol"))
                self.active_identity = {"language": language, "access": access}
                yield f"http://127.0.0.1:{ready['port']}"
            except Exception as error:
                diagnostics.seek(0)
                error.add_note(f"{language}/{access or 'no database'} stderr: {diagnostics.read()[-6000:]}")
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                if lease is not None:
                    lease.process_finished(process)
                reader.join(timeout=3)
                process.stdout.close()

    def request(self, base, path, body=None, status=200):
        payload = body if isinstance(body, bytes) else None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(base + path, data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                actual, headers, content = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            try:
                actual, headers, content = error.code, error.headers, error.read()
            finally:
                error.close()
        self.requests.append(dict(self.active_identity, **{"method": "GET" if body is None else "POST", "path": path,
                              "expected_status": status, "actual_status": actual}))
        self.assertEqual(status, actual, (path, content[:300]))
        return headers, content

    def json_request(self, base, path, body=None, status=200):
        headers, content = self.request(base, path, body, status)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        return json.loads(content)

    def test_h01_h02_h03_return_full_canonical_values(self):
        for language in ("csharp", "java"):
            with self.subTest(language=language), self.server(language) as base:
                headers, body = self.request(base, "/plaintext")
                self.assertIn("text/plain", headers.get("Content-Type", ""))
                self.assertEqual(b"0123456789abcdef0123456789abcdef", body)
                for size in (1024, 65536, 1048576):
                    document = (self.data / f"document-{size}.json").read_bytes()
                    expected = json.loads(document)
                    self.assertEqual(expected, self.json_request(base, f"/json?size={size}"))
                    self.assertEqual(expected, self.json_request(base, f"/json?size={size}", document))
                _, checksum = self.request(base, "/compute")
                self.assertEqual(expected_suite(self.data, "C01", "transform")["checksum"], checksum.decode())
                self.assertEqual({"active": 0}, self.json_request(base, "/__benchmark/active"))
                self.assertEqual({"active": 0}, self.json_request(base, "/__benchmark/quiesce", {}))
                self.request(base, "/plaintext", status=503)
                self.assertEqual({"status": "ready"}, self.json_request(base, "/__benchmark/reset", {}))
                self.assertEqual(b"0123456789abcdef0123456789abcdef", self.request(base, "/plaintext")[1])

    def test_sqlite_h04_h05_h06_match_oracle_rollback_and_reset(self):
        for language in ("csharp", "java"):
            for access in ("raw", "orm"):
                with self.subTest(language=language, access=access), self.server(language, access) as base:
                    oracle = Oracle(self.data)
                    metadata = self.json_request(base, "/__benchmark/metadata")
                    self.assertEqual("sqlite", metadata["engine"])
                    self.assertEqual(access, metadata["access"])
                    initial = oracle.state()
                    self.assertEqual(initial, self.json_request(base, "/__benchmark/state"))
                    self.assertEqual(oracle.execute("D01", {"customer_id": 1}), self.json_request(base, "/customer/1"))
                    self.assertEqual(oracle.execute("D03", {"order_id": 1}), self.json_request(base, "/order/1"))
                    def order(identifier):
                        lines = [{"line_id": identifier * 10 + index, "line_no": index,
                                  "product_id": product["id"], "quantity": 1,
                                  "unit_price_cents": product["price_cents"]}
                                 for index, product in enumerate(list(oracle.tables["product"].values())[:5], 1)]
                        return {"order_id": identifier, "customer_id": 1, "status": 0,
                                "created_epoch_ms": 1700000000000,
                                "total_cents": sum(line["unit_price_cents"] for line in lines), "lines": lines}
                    inserted = order(9000001)
                    self.assertEqual(oracle.execute("D06", inserted), self.json_request(base, "/order", inserted, 201))
                    self.assertEqual(oracle.execute("D03", {"order_id": inserted["order_id"]}), self.json_request(base, "/order/9000001"))
                    failed = dict(order(9000002), inject_failure=True)
                    self.assertEqual({"error": "operation_failed"}, self.json_request(base, "/checkout", failed, 500))
                    self.assertEqual(oracle.state(), self.json_request(base, "/__benchmark/state"))
                    self.assertEqual(oracle.execute("D01", {"customer_id": 1}), self.json_request(base, "/customer/1"))
                    checkout = order(9000003)
                    self.assertEqual(oracle.execute("H06", checkout), self.json_request(base, "/checkout", checkout, 201))
                    self.assertEqual(oracle.execute("D01", {"customer_id": 1}), self.json_request(base, "/customer/1"))
                    self.assertEqual(oracle.execute("D03", {"order_id": checkout["order_id"]}), self.json_request(base, "/order/9000003"))
                    self.assertEqual(oracle.state(), self.json_request(base, "/__benchmark/state"))
                    self.assertEqual([1, 9000001, 9000003], self.json_request(base, "/__benchmark/committed", [9000003, 9000002, 1, 9000001]))
                    self.assertEqual({"active": 0}, self.json_request(base, "/__benchmark/active"))
                    self.assertEqual({"active": 0}, self.json_request(base, "/__benchmark/quiesce", {}))
                    self.request(base, "/plaintext", status=503)
                    self.assertEqual(initial, self.json_request(base, "/__benchmark/reset", {}))
                    self.assertEqual(b"0123456789abcdef0123456789abcdef", self.request(base, "/plaintext")[1])
                    self.assertEqual(initial, self.json_request(base, "/__benchmark/state"))

    def test_malformed_http_returns_consistent_sanitized_errors(self):
        document = json.loads((self.data / "document-1024.json").read_bytes())
        extra = dict(document, unknown="unexpected")
        for language in ("csharp", "java"):
            with self.subTest(language=language), self.server(language) as base:
                for path, body in (("/json?size=0", None), ("/json?size=0", document),
                                   ("/json?size=1024", b"{"), ("/json?size=1024", {}),
                                   ("/json?size=1024", extra), ("/json?size=1024", b'{"name":"' + bytes([255]) + b'"}')):
                    with self.subTest(path=path, body_type=type(body).__name__):
                        self.assertEqual({"error": "invalid_request"}, self.json_request(base, path, body, 400))


if __name__ == "__main__":
    unittest.main()
