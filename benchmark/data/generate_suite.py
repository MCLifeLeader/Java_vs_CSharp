"""Shared deterministic suite fixtures; generate outside measured workers."""
import argparse
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.generate import generate, generate_values


def write_csv(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(header)
        writer.writerows(rows)


def varint(value):
    value &= (1 << 64) - 1
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result + bytes([value]))


def generate_suite(output, scale="smoke", seed=20261025, http_operations=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    generate(output, scale, seed)
    small = scale == "smoke"
    if http_operations is None:
        http_operations = 100 if small else 250000
    if type(http_operations) is not int or http_operations < 1:
        raise ValueError("HTTP trace operation count must be a positive integer")
    customers, products, orders = (100, 32, 200) if small else (10000, 1000, 100000)
    write_csv(output / "customers.csv", ["id", "email", "name", "balance_cents", "version"],
              ((i, f"customer{i:08}@benchmark.invalid", f"Customer {i:08}", 100000, 0) for i in range(1, customers + 1)))
    write_csv(output / "products.csv", ["id", "sku", "name", "price_cents"],
              ((i, f"SKU{i:08}", f"Product {i:08}", 100 + i) for i in range(1, products + 1)))
    randoms = generate_values(orders * 6, seed)
    order_rows, line_rows = [], []
    for order in range(1, orders + 1):
        customer = (randoms[(order - 1) * 6] & ((1 << 64) - 1)) % customers + 1
        total = 0
        for line in range(1, 6):
            product = (randoms[(order - 1) * 6 + line] & ((1 << 64) - 1)) % products + 1
            quantity, price = (order + line) % 5 + 1, 100 + product
            total += quantity * price
            line_rows.append(((order - 1) * 5 + line, order, line, product, quantity, price))
        order_rows.append((order, customer, (order - 1) % 4, 1700000000000 + order * 1000, total, 0))
    write_csv(output / "orders.csv", ["id", "customer_id", "status", "created_epoch_ms", "total_cents", "version"], order_rows)
    write_csv(output / "order_lines.csv", ["id", "order_id", "line_no", "product_id", "quantity", "unit_price_cents"], line_rows)
    # D08 is finite: never recycle writes or delete already consumed rows.
    mix = ["D01"] * 60 + ["D02"] * 15 + ["D05"] * 10 + ["D06"] * 10 + ["D07"] * 5
    for phase_index, phase in enumerate(("warmup", "measurement")):
        trace, deletes = [], 0
        for index in range(100 if small else 50000):
            case = mix[index % 100]
            order_id = phase_index * (orders // 2) + deletes + 1
            if case == "D07": deletes += 1
            trace.append({"case_id": case, "customer_id": index % customers + 1,
                          "order_id": order_id, "insert_id": 1000000000 + phase_index * 10000000 + index,
                          "delta_cents": 7, "worker_partition": (index % customers) % 32})
        if deletes > orders // 2: raise ValueError("Delete trace population exhausted")
        (output / f"trace-{phase}.json").write_text(json.dumps(trace, separators=(",", ":")) + "\n", encoding="utf-8")
        # HTTP inserts/checkouts need independent finite IDs without the DAL
        # delete-population limit. Reads may share this same deterministic trace.
        http_trace = [{"customer_id": index % customers + 1,
                       "insert_id": 3000000000 + phase_index * 1000000000 + index,
                       "worker_partition": (index % customers) % 32}
                      for index in range(http_operations)]
        (output / f"http-trace-{phase}.json").write_text(json.dumps(http_trace, separators=(",", ":")) + "\n", encoding="utf-8")
    ascii_pattern = b"benchmark alpha,beta 123 gamma -42 delta_7\n"
    ascii_bytes = (ascii_pattern * (1048576 // len(ascii_pattern) + 1))[:1048576]
    (output / "ascii.txt").write_bytes(ascii_bytes)
    (output / "unicode.txt").write_text("benchmark Zoë 東京 世界 🌍 123 -42\n" * (128 if small else 32768), encoding="utf-8", newline="")
    (output / "text-1m.bin").write_bytes(ascii_bytes)
    random_bytes = b"".join(struct.pack("<q", value) for value in generate_values(1048576 // 8, seed + 1))
    (output / "random-1m.bin").write_bytes(random_bytes)
    for name in ("text-1m", "random-1m"):
        (output / (name + ".gz")).write_bytes(gzip.compress((output / (name + ".bin")).read_bytes(), compresslevel=6, mtime=0))
    (output / "integers.txt").write_text("\n".join(str(i % 1000 - 500) for i in range(1000 if small else 100000)) + "\n", encoding="ascii")
    base = {"id": 9223372036854775806, "name": "Zoë 東京 🌍", "group": 3, "amount_cents": -12345,
            "active": True, "children": [{"id": 1, "label": 'a,"b'}, {"id": 2, "label": "雪"}], "padding": ""}
    for size in (1024, 65536, 1048576):
        value = dict(base)
        count = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        value["padding"] = "a" * (size - count)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert len(payload) == size
        (output / f"document-{size}.json").write_bytes(payload)
    name = base["name"].encode("utf-8")
    packed = b"".join(varint(value) for value in [0, 1, -1, 9223372036854775807])
    proto = b"\x08" + varint(base["id"]) + b"\x12" + varint(len(name)) + name + b"\x1a" + varint(len(packed)) + packed + b"\x20\x01"
    (output / "protobuf.bin").write_bytes(proto)
    rows = 100 if small else 100000
    target = 8192 if small else 10 * 1024 * 1024
    def csv_payload(padding, tail=0):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(["id", "name", "group", "amount_cents", "active"])
        for i in range(1, rows + 1):
            writer.writerow([i, f'Name {i}, "雪"\nline' + 'x' * (padding + (tail if i == rows else 0)), i % 10, i * 17, str(i % 2 == 0).lower()])
        return stream.getvalue().encode("utf-8")
    pad, tail = divmod(target - len(csv_payload(0)), rows)
    if pad < 0: raise ValueError("CSV target too small")
    csv_bytes = csv_payload(pad, tail)
    assert len(csv_bytes) == target
    (output / "csv-10m.csv").write_bytes(csv_bytes)
    canonical_names = {
        "integers.i64le", "manifest.json", "integers.txt", "customers.csv", "products.csv", "orders.csv", "order_lines.csv",
        "trace-warmup.json", "trace-measurement.json", "http-trace-warmup.json", "http-trace-measurement.json", "ascii.txt", "unicode.txt", "text-1m.bin", "random-1m.bin",
        "text-1m.gz", "random-1m.gz", "document-1024.json", "document-65536.json", "document-1048576.json", "protobuf.bin", "csv-10m.csv"
    }
    manifest = {"contract_version": "suite-1", "scale": scale, "seed": seed, "http_trace_operations": http_operations,
                "rows": {"customers": customers, "products": products, "orders": orders, "order_lines": orders * 5, "csv": rows},
                "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in sorted(output.iterdir()) if p.is_file() and p.name in canonical_names}}
    (output / "suite-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", choices=("smoke", "S"), default="smoke")
    parser.add_argument("--seed", type=int, default=20261025)
    parser.add_argument("--http-operations", type=int, help="Finite per-phase HTTP request population; defaults smoke100/S250000")
    args = parser.parse_args()
    print(json.dumps(generate_suite(args.output, args.scale, args.seed, args.http_operations), indent=2))
