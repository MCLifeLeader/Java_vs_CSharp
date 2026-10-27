"""Independent, arbitrary-precision reference operations, never timed."""

import hashlib
import struct

MASK = (1 << 64) - 1


def expected(values, case_id, parameter_id):
    if (case_id, parameter_id) == ("C01", "transform"):
        total = 0
        for value in values:
            x = ((value + 0x9E3779B97F4A7C15) * 0xBF58476D1CE4E5B9) & MASK
            x ^= x // (1 << 29)
            rotated = ((x * (1 << 17)) & MASK) | (x // (1 << 47))
            total += rotated
    elif (case_id, parameter_id) == ("K01", "scan"):
        total = sum(values)
    elif (case_id, parameter_id) == ("K01", "sort"):
        total = sum(index * value for index, value in enumerate(sorted(values), 1))
    else:
        raise ValueError(f"No oracle for {case_id}/{parameter_id}")
    def digest(sequence):
        value_hash = hashlib.sha256()
        for value in sequence:
            value_hash.update(struct.pack("<q", value))
        return value_hash.hexdigest()
    return {"status": "ok", "case_id": case_id, "parameter_id": parameter_id,
            "items": len(values), "checksum": f"{total & MASK:016x}",
            "input_sha256": digest(values),
            "output_sha256": digest(sorted(values)) if (case_id, parameter_id) == ("K01", "sort") else None,
            "measured": False}
