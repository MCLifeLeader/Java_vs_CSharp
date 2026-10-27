"""Independent untimed reference model for protocol2 workload outputs."""
from decimal import Decimal, ROUND_HALF_EVEN
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import struct
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MASK = (1 << 64) - 1


def fnv(payload):
    value = 14695981039346656037
    for byte in payload:
        value = ((value ^ byte) * 1099511628211) & MASK
    return value


def expected_suite(data, case, parameter, *, scale="smoke", workers=1, profile="controlled"):
    data = Path(data)
    small = scale == "smoke"
    n = 1000 if small else 100000
    scalar, payload, unit, units, details = None, None, "items", n, None
    integers = None
    if case in {"C01", "K01"}:
        integers = [value[0] for value in struct.iter_unpack("<q", (data / "integers.i64le").read_bytes())]
        units = len(integers)
    if case == "C01":
        from validation.oracle import expected
        scalar = int(expected(integers, "C01", "transform")["checksum"], 16)
    elif case == "C02":
        size = 16 if small else 128 if parameter == "n128" else 512
        matrix = []
        for i in range(size):
            for j in range(size):
                value = 0.0
                for k in range(size):
                    value += ((i * 17 + k * 13) % 97) / 97.0 * (((k * 7 + j * 19) % 89) / 89.0)
                matrix.append(value)
        scalar = round(sum(matrix) * 1000000)
        units, unit, details = 2 * size ** 3, "flops", {"n": size, "values": matrix, "absolute_tolerance": 1e-9, "relative_tolerance": 1e-9}
    elif case == "C03":
        total = Decimal(0)
        for i in range(n):
            subtotal = (Decimal(100 + i % 10000) / 100 * (1 + i % 5) * (1 - Decimal(i % 7) / 100)).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
            total += subtotal + (subtotal * Decimal(".0825")).quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
        scalar, unit = int(total * 100), "invoices"
    elif case == "C04":
        value = pow((1 << 1024) + 12345, 65537, (1 << 2048) - 159)
        payload, units, unit = value.to_bytes((value.bit_length() + 7) // 8, "big"), 1, "operations"
    elif case == "C05":
        units, unit = int(parameter[5:]), "bytes"
        payload = hashlib.sha256((data / "random-1m.bin").read_bytes()[:units]).digest()
    elif case == "C06":
        payload = (data / ("random-1m.bin" if parameter.endswith("incompressible") else "text-1m.bin")).read_bytes()
        units, unit = len(payload), "bytes"
    elif case == "K01":
        if parameter == "scan": scalar = sum(integers)
        else:
            ordered = sorted(integers)
            scalar = sum(i * value for i, value in enumerate(ordered, 1))
            payload = b"".join(struct.pack("<q", value) for value in ordered)
    elif case == "K02": scalar = sum(range(0, n, 2)) if parameter == "remove" else n * (n - 1) // 2
    elif case == "K03":
        scalar = sum(i * 3 for i in range(n) if not parameter.endswith("remove") or i % 2)
        units = n * 2 if parameter.endswith("lookup") else n
    elif case == "K04":
        values = list(range(n + n // 2)) if parameter.startswith("union") else list(range(n // 2, n))
        scalar, units = sum(values), len(values)
        payload = "\n".join(str(value) for value in values).encode()
    elif case == "K05":
        payload = (data / ("ascii.txt" if parameter.endswith("ascii") else "unicode.txt")).read_bytes()
        units, unit = len(payload), "bytes"
        if parameter.startswith("search"):
            scalar = payload.decode("utf-8", errors="strict").count("benchmark" if parameter.endswith("ascii") else "世界")
            payload = None
    elif case == "K06":
        if parameter == "integer-parse": scalar = sum(map(int, (data / "integers.txt").read_text().splitlines()))
        else:
            text = (data / "ascii.txt").read_bytes()
            scalar, units, unit = sum(len(match) for match in re.findall(rb"[A-Za-z]+|[0-9]+", text)), len(text), "bytes"
    elif case == "M01": scalar = n * (n - 1) // 2
    elif case in {"M02", "M05"}: scalar, units = 10 * n * (n - 1) // 2, n * 10
    elif case == "M03":
        count = 10000 if small else 1000000
        scalar, units = 10 * count * (count - 1) // 2, count * 10
    elif case == "M04":
        units, unit = int(parameter.split('-')[1]), "bytes"
        scalar = sum((data / "random-1m.bin").read_bytes()[:units])
    elif case == "A01":
        units, unit = 1000 if small else 100000, "tasks"
        scalar = sum((i + 1) * (i + 3) for i in range(units))
    elif case == "A02":
        units, unit = 1000 if small else 100000, "messages"
        scalar = units * (units - 1) // 2
    elif case == "A03": scalar = units = workers * (100 if small else 10000); unit = "increments"
    elif case == "A04":
        scalar = units = (32 if parameter.endswith("10000") else 16) if small else int(parameter.split('-')[1])
        unit = "requests"
    elif case == "A05": scalar, units, unit = 0, 8, "children"
    elif case == "S01":
        payload = (data / f"document-{parameter.split('-')[1]}.json").read_bytes()
        parsed = json.loads(payload)
        assert parsed["id"] == 9223372036854775806 and parsed["children"][1]["label"] == "雪"
        units, unit = len(payload), "bytes"
    elif case == "S02": payload = (data / "protobuf.bin").read_bytes(); units, unit = len(payload), "bytes"
    elif case == "S03":
        sums = {group: sum(i * 6 for i in range(n) if i % 2 == 0 and i % 17 == group) for group in range(17)}
        payload = "\n".join(f"{key}:{value}" for key, value in sums.items()).encode(); unit = "records"
    elif case == "S04":
        with (data / "csv-10m.csv").open(encoding="utf-8", newline="") as source: rows = list(csv.reader(source, strict=True))
        target = io.StringIO(newline=""); csv.writer(target, lineterminator="\r\n").writerows(rows)
        payload, units, unit = target.getvalue().encode(), len(rows) - 1, "rows"
    elif case == "I01":
        units, unit = 65536 if small else 1 << 30, "bytes"
        source = (data / "random-1m.bin").read_bytes()
        quotient, remaining = divmod(units, len(source)); scalar = quotient * sum(source) + sum(source[:remaining])
    elif case == "I02":
        units, unit = 16 if small else 256, "reads"
        source = (data / "random-1m.bin").read_bytes(); length = 65536 if small else 1 << 30
        scalar = sum(source[((i * 7919 % (length // 4096)) * 4096) % len(source)] for i in range(units))
    elif case == "I03" or case == "I04" and parameter != "handshake":
        payload = (data / "random-1m.bin").read_bytes(); units, unit = len(payload), "bytes"
    elif case == "I04": scalar, units, unit = 1, 1, "handshakes"
    elif case == "N01":
        units, unit = 1 if parameter == "count-1" else 1024, "integers"; scalar = sum(i - 512 for i in range(units))
    elif case == "N02": scalar, units, unit = sum((data / "random-1m.bin").read_bytes()), 1048576, "bytes"
    elif case == "L01": scalar = n * (n - 1) // 2
    elif case == "L02": scalar = sum((i + 1, i * 2, i ^ 85, i - 3)[i % 4] for i in range(n))
    elif case == "L03":
        threshold = {"failure-0pct": 0, "failure-0.1pct": 1, "failure-1pct": 10, "failure-10pct": 100}[parameter]
        scalar = sum(1 if i % 1000 < threshold else i for i in range(n))
    elif case == "L04": scalar = sum(i * 3 + 1 + (7 if parameter == "invoke-capturing" else 0) for i in range(n))
    else: raise ValueError(f"No independent oracle: {case}/{parameter}")
    if scalar is None: scalar = fnv(payload)
    scalar &= MASK
    if payload is None: payload = str(scalar).encode()
    answer = {"checksum": f"{scalar:016x}", "semantic_hash": hashlib.sha256(payload).hexdigest(), "work_units": units, "unit": unit}
    if details is not None: answer["details"] = details
    return answer


def write_oracles(data, scale="smoke"):
    root = Path(__file__).resolve().parents[1]
    cases = json.loads((root / "config/cases.json").read_text())["cases"]
    supported = set("C K M A S I N L".split())
    output = {}
    for case in cases:
        if case["case_id"][0] not in supported or case["case_id"] == "L05": continue
        for parameter in case["parameters"]:
            key = f"{case['case_id']}/{parameter['parameter_id']}"
            output[key] = expected_suite(data, case["case_id"], parameter["parameter_id"], scale=scale)
            if case["case_id"] == "A03": output[key]["by_workers"] = {str(w): expected_suite(data, case["case_id"], parameter["parameter_id"], scale=scale, workers=w) for w in [1, 4, 8, 32]}
    (Path(data) / "oracle.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--data", type=Path, required=True); parser.add_argument("--scale", default="smoke", choices=("smoke", "S")); args = parser.parse_args()
    print(json.dumps({"oracle_entries": len(write_oracles(args.data, args.scale))}))
