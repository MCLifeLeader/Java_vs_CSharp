"""Independent dictionary oracle for canonical database inputs; no worker/provider code."""
import copy
import csv
import hashlib
from pathlib import Path

class Oracle:
    def __init__(self, data_directory):
        self.tables = {}
        for file, table in [("customers.csv", "customer"), ("products.csv", "product"),
                            ("orders.csv", "orders"), ("order_lines.csv", "order_line")]:
            with (Path(data_directory) / file).open(encoding="utf-8", newline="") as stream:
                self.tables[table] = {int(r["id"]): {k: v if k in ("email", "sku", "name") else int(v)
                                                    for k, v in r.items()} for r in csv.DictReader(stream)}
        self._index_lines()

    def _index_lines(self):
        self.lines_by_order = {}
        for line in self.tables["order_line"].values():
            self.lines_by_order.setdefault(line["order_id"], set()).add(line["id"])

    def state(self):
        digest = hashlib.sha256()
        for table, rows in self.tables.items():
            for key in sorted(rows):
                digest.update((table + "\t" + "\t".join(str(x) for x in rows[key].values()) + "\n").encode())
        return {"sha256": digest.hexdigest(), "counts": {t: len(rows) for t, rows in self.tables.items()}}

    def execute(self, case, p):
        before = copy.deepcopy(self.tables)
        try:
            result = self._execute(case, p)
            if p.get("inject_failure"):
                raise ValueError("injected")
            return copy.deepcopy(result)
        except Exception:
            self.tables = before
            self._index_lines()
            raise

    def _execute(self, case, p):
        customers, products, orders, lines = (self.tables[t] for t in ("customer", "product", "orders", "order_line"))
        if case in ("D01", "H04"):
            return customers[p["customer_id"]]
        if case == "D02":
            return sorted((o for o in orders.values() if o["status"] == p["status"] and
                           (o["created_epoch_ms"], o["id"]) > (p["cursor_created_epoch_ms"], p["cursor_id"])),
                          key=lambda o: (o["created_epoch_ms"], o["id"]))[:50]
        if case in ("D03", "H05-read"):
            order = copy.deepcopy(orders[p["order_id"]])
            order["customer"] = customers[order["customer_id"]]
            order["lines"] = sorted((lines[key] for key in self.lines_by_order.get(order["id"], ())), key=lambda l: l["line_no"])
            return order
        if case == "D04":
            groups = []
            for status in range(4):
                rows = [o for o in orders.values() if o["status"] == status and
                        p["start_epoch_ms"] <= o["created_epoch_ms"] < p["end_epoch_ms"]]
                if rows:
                    groups.append({"status": status, "order_count": len(rows), "total_cents": sum(o["total_cents"] for o in rows)})
            return groups
        if case == "D05":
            customer = customers[p["customer_id"]]
            if p.get("expected_version", customer["version"]) != customer["version"]:
                raise ValueError("stale")
            customer["balance_cents"] += p["delta_cents"]
            customer["version"] += 1
            return customer
        if case in ("D06", "H05-write", "H06"):
            if p["order_id"] in orders:
                raise ValueError("duplicate")
            new_lines = [{"id": l["line_id"], "order_id": p["order_id"], "line_no": l["line_no"],
                          "product_id": l["product_id"], "quantity": l["quantity"],
                          "unit_price_cents": l["unit_price_cents"]} for l in p["lines"]]
            if sum(l["quantity"] * l["unit_price_cents"] for l in new_lines) != p["total_cents"]:
                raise ValueError("wrong total")
            order = {"id": p["order_id"], "customer_id": p["customer_id"], "status": p["status"],
                     "created_epoch_ms": p["created_epoch_ms"], "total_cents": p["total_cents"], "version": 0}
            if case == "H06":
                for line in new_lines:
                    if products[line["product_id"]]["price_cents"] != line["unit_price_cents"]:
                        raise ValueError("wrong product price")
                customer = customers[p["customer_id"]]
                customer["balance_cents"] -= p["total_cents"]
                customer["version"] += 1
            orders[order["id"]] = order
            for line in new_lines:
                if line["id"] in lines:
                    raise ValueError("duplicate line")
                lines[line["id"]] = line
                self.lines_by_order.setdefault(order["id"], set()).add(line["id"])
            if case == "H06":
                return {"id": order["id"], "total_cents": order["total_cents"], "version": customer["version"]}
            return dict(order, lines=new_lines)
        if case == "D07":
            order = orders[p["order_id"]]
            if p.get("expected_version", order["version"]) != order["version"]:
                raise ValueError("stale")
            for key in self.lines_by_order.pop(order["id"], ()):
                del lines[key]
            del orders[order["id"]]
            return {"order_id": order["id"], "deleted_lines": 5, "deleted": True}
        if case == "D08":
            results = []
            for operation in p["trace"]:
                try:
                    results.append(self.execute(operation["case_id"], operation["input"]))
                except ValueError as error:
                    results.append({"status": "error", "category": "conflict" if str(error) == "stale" else "constraint"})
            return results
        raise ValueError(case)


def new_order(key, **extra):
    lines = [{"line_id": key * 5 + i, "line_no": i, "product_id": i, "quantity": 1,
              "unit_price_cents": 100 + i} for i in range(1, 6)]
    return dict({"order_id": key, "customer_id": 1, "status": 0, "created_epoch_ms": 1800000000000,
                 "total_cents": 515, "lines": lines}, **extra)
