"""Verify indexed independent-oracle mutation and rollback semantics."""
import csv
from pathlib import Path
import tempfile
import unittest

from database_oracle import Oracle, new_order


class DatabaseOracleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        order = new_order(1)
        tables = {
            "customers.csv": [{"id": 1, "email": "one@benchmark.invalid", "name": "One", "balance_cents": 100000, "version": 0}],
            "products.csv": [{"id": i, "sku": f"SKU{i}", "name": f"Product {i}", "price_cents": 100+i} for i in range(1, 6)],
            "orders.csv": [{"id": 1, "customer_id": 1, "status": 0, "created_epoch_ms": order["created_epoch_ms"], "total_cents": 515, "version": 0}],
            "order_lines.csv": [{"id": line["line_id"], "order_id": 1, **{key: value for key, value in line.items() if key != "line_id"}} for line in order["lines"]],
        }
        for name, rows in tables.items():
            with (directory / name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        self.oracle = Oracle(directory)
        self.initial = self.oracle.state()
        self.graph = self.oracle.execute("D03", {"order_id": 1})

    def test_known_valid_insert_delete_keeps_index_and_state_consistent(self):
        for key in range(2, 102):
            self.oracle._execute("D06", new_order(key))
        for key in range(2, 102):
            self.assertEqual(5, len(self.oracle._execute("D03", {"order_id": key})["lines"]))
            self.oracle._execute("D07", {"order_id": key})
        self.assertEqual(self.initial, self.oracle.state())
        self.assertEqual(self.graph, self.oracle.execute("D03", {"order_id": 1}))

    def test_partial_duplicate_insert_rolls_back_rows_and_index(self):
        order = new_order(2)
        order["lines"][2]["line_id"] = self.graph["lines"][0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate line"):
            self.oracle.execute("D06", order)
        self.assertEqual(self.initial, self.oracle.state())
        self.assertNotIn(2, self.oracle.lines_by_order)
        self.assertEqual(self.graph, self.oracle.execute("D03", {"order_id": 1}))

    def test_injected_delete_rollback_restores_line_index(self):
        with self.assertRaisesRegex(ValueError, "injected"):
            self.oracle.execute("D07", {"order_id": 1, "inject_failure": True})
        self.assertEqual(self.initial, self.oracle.state())
        self.assertEqual(self.graph, self.oracle.execute("D03", {"order_id": 1}))


if __name__ == "__main__":
    unittest.main()
