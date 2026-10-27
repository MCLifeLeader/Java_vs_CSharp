import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data.generate import generate, generate_values
from validation.oracle import expected


class GeneratorTests(unittest.TestCase):
    def test_splitmix64_published_seed_zero_vector(self):
        # Fixed SplitMix64 seed-zero sequence, independently fixed from our workload seed.
        unsigned = [value & ((1 << 64) - 1) for value in generate_values(3, 0)]
        self.assertEqual(unsigned, [0xE220A8397B1DCDAF, 0x6E789E6AA1B965F4, 0x06C45D188009454F])

    def test_same_seed_produces_identical_files_and_hashes(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            a = generate(first)
            b = generate(second)
            self.assertEqual(a, b)
            payload = (Path(first) / a["file"]).read_bytes()
            self.assertEqual(payload, (Path(second) / b["file"]).read_bytes())
            self.assertEqual(hashlib.sha256(payload).hexdigest(), a["sha256"])
            self.assertEqual(len(payload), a["items"] * 8)
            self.assertEqual(a, json.loads((Path(first) / "manifest.json").read_text()))
            values = [item[0] for item in struct.iter_unpack("<q", payload)]
            self.assertTrue(any(value < 0 for value in values))
            self.assertTrue(any(value > 0 for value in values))

    def test_distinct_seeds_produce_distinct_inputs(self):
        self.assertNotEqual(generate_values(32, 1), generate_values(32, 2))

    def test_nonpositive_counts_rejected(self):
        for count in [0, -1, 2.5]:
            with self.subTest(count=count), self.assertRaises(ValueError):
                generate_values(count)

    def test_oracle_signed_sort_and_modular_sum(self):
        values = [-(1 << 63), -1, 0, 1, (1 << 63) - 1]
        self.assertEqual(expected(values, "K01", "scan")["checksum"], "ffffffffffffffff")
        self.assertEqual(expected(values, "K01", "sort")["checksum"], "fffffffffffffffd")

    def test_oracle_unknown_case_rejected(self):
        with self.assertRaises(ValueError):
            expected([1], "D01", "lookup")


if __name__ == "__main__":
    unittest.main()
