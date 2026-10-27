import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.generate_suite import generate_suite
from validation.suite_oracle import expected_suite


class SuiteFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.data = Path(cls.directory.name)
        cls.manifest = generate_suite(cls.data)

    @classmethod
    def tearDownClass(cls): cls.directory.cleanup()

    def test_manifest_is_reproducible_despite_runtime_files(self):
        (self.data / "services.json").write_text('{"port":1234}')
        (self.data / "oracle.json").write_text('{"runtime":"diagnostic"}')
        repeated = generate_suite(self.data)
        self.assertEqual(self.manifest, repeated)
        self.assertNotIn("services.json", repeated["files"])
        for name, item in repeated["files"].items():
            self.assertEqual(hashlib.sha256((self.data / name).read_bytes()).hexdigest(), item["sha256"])

    def test_json_exact_sizes_and_numeric_unicode_boundaries(self):
        for size in [1024, 65536, 1048576]:
            with self.subTest(size=size):
                payload = (self.data / f"document-{size}.json").read_bytes()
                self.assertEqual(len(payload), size)
                value = json.loads(payload)
                self.assertEqual(value["id"], 9223372036854775806)
                self.assertEqual(value["name"], "Zoë 東京 🌍")
                self.assertEqual(value["children"][0]["label"], 'a,"b')

    def test_relational_foreign_keys_five_lines_and_totals(self):
        def read(name):
            with (self.data / name).open(encoding="utf-8", newline="") as source: return list(csv.DictReader(source))
        customers = {int(row["id"]) for row in read("customers.csv")}
        products = {int(row["id"]): int(row["price_cents"]) for row in read("products.csv")}
        lines = read("order_lines.csv")
        for order in read("orders.csv"):
            self.assertIn(int(order["customer_id"]), customers)
            graph = [line for line in lines if line["order_id"] == order["id"]]
            self.assertEqual([int(line["line_no"]) for line in graph], [1, 2, 3, 4, 5])
            self.assertEqual(int(order["total_cents"]), sum(int(line["quantity"]) * int(line["unit_price_cents"]) for line in graph))
            self.assertTrue(all(int(line["unit_price_cents"]) == products[int(line["product_id"])] for line in graph))

    def test_mixed_traces_preserve_proportions_and_write_ownership(self):
        phases = {}
        for phase in ["warmup", "measurement"]:
            trace = json.loads((self.data / f"trace-{phase}.json").read_text())
            self.assertEqual({case: sum(item["case_id"] == case for item in trace) for case in ["D01", "D02", "D05", "D06", "D07"]},
                             {"D01": 60, "D02": 15, "D05": 10, "D06": 10, "D07": 5})
            phases[phase] = trace
            ownership = {}
            for item in trace:
                self.assertEqual(item["worker_partition"], (item["customer_id"] - 1) % 32)
                ownership.setdefault(item["customer_id"], item["worker_partition"])
                self.assertEqual(ownership[item["customer_id"]], item["worker_partition"])
        for field in ["order_id", "insert_id"]:
            relevant = "D07" if field == "order_id" else "D06"
            a = {item[field] for item in phases["warmup"] if item["case_id"] == relevant}
            b = {item[field] for item in phases["measurement"] if item["case_id"] == relevant}
            self.assertFalse(a & b)

    def test_csv_has_embedded_newlines_quotes_and_exact_scale(self):
        self.assertEqual((self.data / "csv-10m.csv").stat().st_size, 8192)
        with (self.data / "csv-10m.csv").open(encoding="utf-8", newline="") as source: rows = list(csv.reader(source, strict=True))
        self.assertEqual(len(rows), 101)
        self.assertIn('\n', rows[1][1])
        self.assertIn('"雪"', rows[1][1])
        self.assertTrue(all(len(row) == 5 for row in rows))

    def test_http_population_is_finite_disjoint_and_independent_of_delete_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            manifest = generate_suite(data, "smoke", http_operations=250)
            self.assertEqual(250, manifest["http_trace_operations"])
            phases = [json.loads((data / f"http-trace-{phase}.json").read_text()) for phase in ("warmup", "measurement")]
            self.assertEqual([250, 250], [len(trace) for trace in phases])
            self.assertEqual(100, len(json.loads((data / "trace-measurement.json").read_text())))
            self.assertFalse({item["insert_id"] for item in phases[0]} & {item["insert_id"] for item in phases[1]})
            self.assertTrue(all(1 <= item["customer_id"] <= 100 for trace in phases for item in trace))
            with self.assertRaises(ValueError): generate_suite(data, "smoke", http_operations=0)

    def test_oracle_distinguishes_workers_and_protocol_units(self):
        one = expected_suite(self.data, "A03", "atomic", workers=1)
        four = expected_suite(self.data, "A03", "atomic", workers=4)
        self.assertEqual(int(four["checksum"], 16), 4 * int(one["checksum"], 16))
        self.assertEqual(four["work_units"], 400)
        with self.assertRaises(ValueError): expected_suite(self.data, "X99", "unknown")
