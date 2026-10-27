"""Synthetic SQLite schema/query checks; never a C#/Java/provider benchmark."""

from pathlib import Path
import sqlite3
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def query(name, engine="sqlite"):
    return (ROOT / "queries" / engine / name).read_text(encoding="utf-8")


def connection(uri):
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA read_uncommitted=0")
    db.execute("PRAGMA busy_timeout=5000")
    return db


class DatabaseContractTests(unittest.TestCase):
    def setUp(self):
        self.uri = f"file:benchmark_contract_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.anchor = connection(self.uri)
        self.anchor.executescript((ROOT / "schema" / "sqlite.sql").read_text(encoding="utf-8"))
        self.db = connection(self.uri)
        self.db.execute("BEGIN")
        self.db.executemany("INSERT INTO customer VALUES (?, ?, ?, ?, ?)", [
            (1, "one@example.test", "Customer 1", 100000, 0),
            (2, "two@example.test", "Customer 2", -100, 0),
        ])
        self.db.executemany("INSERT INTO product VALUES (?, ?, ?, ?)", [
            (i, f"SKU{i}", f"Product {i}", i * 100) for i in range(1, 6)
        ])
        self.orders = []
        for order_id in range(1, 241):
            self.insert_graph(order_id)
            self.orders.append({"id": order_id, "status": order_id % 4,
                                "created_epoch_ms": 1000 + order_id // 8, "total_cents": 5500})
        self.db.execute("COMMIT")

    def tearDown(self):
        self.db.close()
        self.anchor.close()

    def insert_graph(self, order_id):
        order = {"order_id": order_id, "customer_id": 1, "status": order_id % 4,
                 "created_epoch_ms": 1000 + order_id // 8, "total_cents": 5500, "version": 0}
        self.db.execute(query("D06-order.sql"), order)
        for i in range(1, 6):
            self.db.execute(query("D06-line.sql"), {
                "line_id": order_id * 5 + i, "order_id": order_id, "line_no": i,
                "product_id": i, "quantity": i, "unit_price_cents": i * 100,
            })

    def counts(self):
        return tuple(self.db.execute(f"SELECT COUNT(id) FROM {table}").fetchone()[0]
                     for table in ("customer", "product", "orders", "order_line"))

    def test_named_memory_lifetime_and_per_connection_settings(self):
        for db in (self.anchor, self.db):
            self.assertEqual(1, db.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(0, db.execute("PRAGMA read_uncommitted").fetchone()[0])
            self.assertEqual(5000, db.execute("PRAGMA busy_timeout").fetchone()[0])
            self.assertEqual("memory", db.execute("PRAGMA journal_mode").fetchone()[0])
        self.db.close()
        self.db = connection(self.uri)
        self.assertEqual((2, 5, 240, 1200), self.counts())
        self.db.close()
        self.anchor.close()
        self.db = connection(self.uri)
        self.assertEqual([], self.db.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall())

    def test_realized_catalog_has_expected_indexes_and_no_cascade(self):
        expected = {
            "ix_orders_customer_created_id": ["customer_id", "created_epoch_ms", "id"],
            "ix_orders_status_created_id": ["status", "created_epoch_ms", "id"],
            "ix_order_line_product_id": ["product_id"],
        }
        for name, columns in expected.items():
            self.assertEqual(columns, [row[2] for row in self.db.execute(f"PRAGMA index_info({name})")])
        for table in ("orders", "order_line"):
            for fk in self.db.execute(f"PRAGMA foreign_key_list({table})"):
                self.assertEqual("NO ACTION", fk[6])
        for table in ("customer", "product", "orders", "order_line"):
            info = self.db.execute(f"PRAGMA table_info({table})").fetchall()
            self.assertTrue(all(row[3] == 1 for row in info))
        self.assertEqual([], self.db.execute("PRAGMA foreign_key_check").fetchall())

    def test_duplicate_foreign_key_and_required_value_failures(self):
        failures = [
            ("INSERT INTO customer VALUES (?, ?, ?, ?, ?)", (1, "new@example.test", "New", 0, 0)),
            ("INSERT INTO customer VALUES (?, ?, ?, ?, ?)", (3, "one@example.test", "New", 0, 0)),
            ("INSERT INTO customer VALUES (?, ?, ?, ?, ?)", (None, "new@example.test", "New", 0, 0)),
            ("INSERT INTO product VALUES (?, ?, ?, ?)", (6, "SKU1", "New", 0)),
            ("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", (300, 999, 0, 1000, 0, 0)),
            ("INSERT INTO order_line VALUES (?, ?, ?, ?, ?, ?)", (9999, 9999, 1, 1, 1, 100)),
            ("INSERT INTO order_line VALUES (?, ?, ?, ?, ?, ?)", (9999, 1, 6, 9999, 1, 100)),
            ("INSERT INTO order_line VALUES (?, ?, ?, ?, ?, ?)", (9999, 1, 1, 1, 1, 100)),
            ("DELETE FROM orders WHERE id=?", (1,)),
            ("DELETE FROM customer WHERE id=?", (1,)),
            ("DELETE FROM product WHERE id=?", (1,)),
        ]
        for statement, parameters in failures:
            with self.subTest(statement=statement, parameters=parameters):
                before = self.counts()
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(statement, parameters)
                self.assertEqual(before, self.counts())

    def test_ascii_lengths_and_case_sensitive_unique_values(self):
        self.db.execute("INSERT INTO customer VALUES (3, 'Case@example.test', 'Case', 0, 0)")
        self.db.execute("INSERT INTO customer VALUES (4, 'case@example.test', 'case', 0, 0)")
        for email in ("", "x" * 255, "nonascii\u00e9@example.test", "trailing ", "a\0b", "line\nbreak"):
            with self.subTest(email=repr(email)):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute("INSERT INTO customer VALUES (5, ?, 'Name', 0, 0)", (email,))
        self.db.execute("INSERT INTO customer VALUES (5, ?, ?, 0, 0)", ("x" * 254, "n" * 128))
        self.db.execute("INSERT INTO product VALUES (6, ?, 'Name', 0)", ("s" * 64,))
        for statement, values in [
            ("INSERT INTO customer VALUES (6, 'six@example.test', ?, 0, 0)", ("n" * 129,)),
            ("INSERT INTO product VALUES (7, ?, 'Name', 0)", ("s" * 65,)),
            ("INSERT INTO product VALUES (7, ?, 'Name', 0)", ("s\0bad",)),
        ]:
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(statement, values)

    def test_signed_64bit_boundaries_and_domain_checks(self):
        maximum, minimum = (1 << 63) - 1, -(1 << 63)
        self.db.execute("INSERT INTO customer VALUES (?, 'max@example.test', 'Max', ?, 0)", (maximum, minimum))
        self.assertEqual(minimum, self.db.execute(query("D01.sql"), {"customer_id": maximum}).fetchone()[3])
        with self.assertRaises(OverflowError):
            self.db.execute("UPDATE customer SET balance_cents=? WHERE id=1", (maximum + 1,))
        for statement in (
            "UPDATE customer SET version=-1 WHERE id=1",
            "UPDATE product SET price_cents=-1 WHERE id=1",
            "UPDATE orders SET status=4 WHERE id=1",
            "UPDATE orders SET total_cents=-1 WHERE id=1",
            "UPDATE orders SET created_epoch_ms=-1 WHERE id=1",
            "UPDATE order_line SET quantity=0 WHERE id=6",
            "UPDATE order_line SET line_no=0 WHERE id=6",
            "UPDATE order_line SET unit_price_cents=-1 WHERE id=6",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.db.execute(statement)

    def test_customer_and_single_query_full_graph_materialization(self):
        customer = dict(self.db.execute(query("D01.sql"), {"customer_id": 1}).fetchone())
        self.assertEqual({"id": 1, "email": "one@example.test", "name": "Customer 1",
                          "balance_cents": 100000, "version": 0}, customer)
        executed = []
        self.db.set_trace_callback(executed.append)
        rows = self.db.execute(query("D03.sql"), {"order_id": 1}).fetchall()
        self.db.set_trace_callback(None)
        self.assertEqual(1, len(executed))
        self.assertEqual(5, len(rows))
        self.assertEqual(list(range(1, 6)), [row["line_no"] for row in rows])
        self.assertEqual(1, len({row["order_id"] for row in rows}))
        self.assertEqual(1, len({row["customer_record_id"] for row in rows}))
        self.assertEqual(5500, sum(row["quantity"] * row["unit_price_cents"] for row in rows))
        self.assertTrue(all(row["total_cents"] == 5500 for row in rows))
        self.assertEqual(17, len(rows[0].keys()))

    def test_keyset_paging_preserves_ties_and_complete_rows(self):
        status = 3
        expected = sorted((o for o in self.orders if o["status"] == status),
                          key=lambda o: (o["created_epoch_ms"], o["id"]))
        first = self.db.execute(query("D02.sql"), {"status": status, "cursor_created_epoch_ms": 0,
                                                     "cursor_id": 0}).fetchall()
        self.assertEqual(50, len(first))
        self.assertEqual(6, len(first[0].keys()))
        last = first[-1]
        rest = self.db.execute(query("D02.sql"), {"status": status,
                                                  "cursor_created_epoch_ms": last["created_epoch_ms"],
                                                  "cursor_id": last["id"]}).fetchall()
        self.assertEqual(10, len(rest))
        self.assertEqual([o["id"] for o in expected], [o["id"] for o in first + rest])

    def test_group_projection_uses_ordered_four_statuses_and_half_open_window(self):
        start, end = 1005, 1020
        rows = self.db.execute(query("D04.sql"), {"start_epoch_ms": start, "end_epoch_ms": end}).fetchall()
        expected = []
        for status in range(4):
            members = [o for o in self.orders if o["status"] == status and start <= o["created_epoch_ms"] < end]
            expected.append((status, len(members), sum(o["total_cents"] for o in members)))
        self.assertEqual(expected, [tuple(row) for row in rows])

    def test_update_read_modify_write_version_increment_and_stale_conflict(self):
        self.db.execute("BEGIN")
        old = self.db.execute(query("D05-read.sql"), {"customer_id": 1}).fetchone()
        parameters = {"customer_id": 1, "new_balance_cents": old["balance_cents"] + 125,
                      "new_version": old["version"] + 1, "old_version": old["version"]}
        self.assertEqual(1, self.db.execute(query("D05-update.sql"), parameters).rowcount)
        self.db.execute("COMMIT")
        self.db.execute("BEGIN")
        self.assertEqual(0, self.db.execute(query("D05-update.sql"), parameters).rowcount)
        self.db.execute("ROLLBACK")
        current = self.db.execute(query("D01.sql"), {"customer_id": 1}).fetchone()
        self.assertEqual(100125, current["balance_cents"])
        self.assertEqual(1, current["version"])

    def test_insert_six_logical_statements_and_failure_rolls_back_full_graph(self):
        before = self.counts()
        self.db.execute("BEGIN")
        executed = []
        self.db.set_trace_callback(executed.append)
        self.insert_graph(10000)
        self.db.set_trace_callback(None)
        self.assertEqual(6, len(executed))
        self.db.execute("COMMIT")
        self.assertEqual((before[0], before[1], before[2] + 1, before[3] + 5), self.counts())
        committed = self.counts()
        self.db.execute("BEGIN")
        self.insert_graph(10001)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(query("D06-line.sql"), {"line_id": 999999, "order_id": 10001,
                "line_no": 1, "product_id": 1, "quantity": 1, "unit_price_cents": 100})
        self.db.execute("ROLLBACK")
        self.assertEqual(committed, self.counts())
        self.assertEqual([], self.db.execute(query("D03.sql"), {"order_id": 10001}).fetchall())

    def test_delete_children_then_stale_parent_rolls_back_then_success_commits(self):
        before = self.counts()
        for stale in (True, False):
            self.db.execute("BEGIN")
            rows = self.db.execute(query("D07-load.sql"), {"order_id": 1}).fetchall()
            self.assertEqual(5, len(rows))
            for row in rows:
                self.assertEqual(1, self.db.execute(query("D07-child.sql"), {
                    "line_id": row["line_id"], "order_id": 1}).rowcount)
            old_version = rows[0]["order_version"] + int(stale)
            affected = self.db.execute(query("D07-parent.sql"), {"order_id": 1,
                                                                "old_version": old_version}).rowcount
            self.assertEqual(0 if stale else 1, affected)
            self.db.execute("ROLLBACK" if stale else "COMMIT")
            if stale:
                self.assertEqual(before, self.counts())
        self.assertEqual((before[0], before[1], before[2] - 1, before[3] - 5), self.counts())

    def test_engine_templates_share_parameters_and_exclude_hidden_shortcuts(self):
        import re
        sqlite_names = {p.name for p in (ROOT / "queries" / "sqlite").glob("*.sql")}
        self.assertEqual(12, len(sqlite_names))
        for engine in ("sqlserver", "postgresql", "sqlite"):
            self.assertEqual(sqlite_names, {p.name for p in (ROOT / "queries" / engine).glob("*.sql")})
            for name in sorted(sqlite_names):
                with self.subTest(engine=engine, name=name):
                    sql = query(name, engine)
                    self.assertNotIn("SELECT *", sql.upper())
                    self.assertNotIn("OFFSET", sql.upper())
                    self.assertEqual(re.findall(r":([a-z_]+)", query(name)), re.findall(r":([a-z_]+)", sql))


if __name__ == "__main__":
    unittest.main()
