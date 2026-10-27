"""Authoritative binary inputs. Only Python generates randomness; workers read bytes."""

import argparse
import hashlib
import json
from pathlib import Path
import struct

MASK = (1 << 64) - 1
SEED = 20261025
COUNTS = {"smoke": 4096, "S": 1_000_000}


def generate_values(count, seed=SEED):
    # SplitMix64 v1: fixed unsigned modulo-2^64 algorithm, serialized as signed int64.
    if not isinstance(count, int) or count <= 0:
        raise ValueError("count must be a positive integer")
    state = seed & MASK
    values = []
    for _ in range(count):
        state = (state + 0x9E3779B97F4A7C15) & MASK
        z = ((state ^ (state >> 30)) * 0xBF58476D1CE4E5B9) & MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK
        z ^= z >> 31
        values.append(z if z < (1 << 63) else z - (1 << 64))
    return values


def generate(output, dataset="smoke", seed=SEED):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    values = generate_values(COUNTS[dataset], seed)
    payload = b"".join(struct.pack("<q", value) for value in values)
    (output / "integers.i64le").write_bytes(payload)
    manifest = {"version": 1, "dataset": dataset, "seed": seed, "algorithm": "SplitMix64-v1",
                "encoding": "signed-int64-little-endian", "items": len(values),
                "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                "file": "integers.i64le", "scope": ["C01", "K01"],
                "database_data": "not implemented"}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", choices=COUNTS, default="smoke")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    print(json.dumps(generate(args.output, args.dataset, args.seed), indent=2))
