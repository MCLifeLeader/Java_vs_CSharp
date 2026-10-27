import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.benchmarkctl import run_command
from data.generate import generate_values
from validation.oracle import expected

COMMANDS = {
    "csharp": ["dotnet", str(ROOT / "dotnet/bin/Release/net10.0/Worker.dll")],
    "java": ["java", "-cp", str(ROOT / "java/build"), "BenchmarkWorker"],
}


class WorkerParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Missing builds are failures; verification must not silently become skipped tests.
        for path in [ROOT / "dotnet/bin/Release/net10.0/Worker.dll", ROOT / "java/build/BenchmarkWorker.class"]:
            if not path.is_file():
                raise RuntimeError(f"Build workers before validation: {path}")

    def invoke(self, language, payload, commands):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integers.i64le"
            path.write_bytes(payload)
            raw = run_command(COMMANDS[language] + [str(path)], stdin=commands, timeout=20)
            result = subprocess.CompletedProcess(COMMANDS[language], raw["exit_code"], raw["stdout"], raw["stderr"])
            return result, [json.loads(line) for line in result.stdout.splitlines()]

    def test_boundary_values_match_independent_oracle(self):
        values = [-(1 << 63), (1 << 63) - 1, -1, 0, 1, -42, 42, 0, -1]
        payload = b"".join(struct.pack("<q", value) for value in values)
        operations = [("C01", "transform"), ("K01", "scan"), ("K01", "sort")]
        commands = "".join(f"CHECK\t{case}\t{parameter}\n" for case, parameter in operations) + "QUIT\n"
        for language in COMMANDS:
            with self.subTest(language=language):
                process, rows = self.invoke(language, payload, commands)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(rows[0]["status"], "ready")
                self.assertEqual(rows[0]["language"], language)
                self.assertEqual(rows[0]["items"], len(values))
                self.assertEqual(rows[1:], [expected(values, *operation) for operation in operations])

    def test_sort_does_not_mutate_runtime_loaded_input(self):
        values = generate_values(4096)
        commands = "CHECK\tC01\ttransform\nCHECK\tK01\tsort\nCHECK\tC01\ttransform\nQUIT\n"
        payload = b"".join(struct.pack("<q", value) for value in values)
        for language in COMMANDS:
            with self.subTest(language=language):
                process, rows = self.invoke(language, payload, commands)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(rows[1], expected(values, "C01", "transform"))
                self.assertEqual(rows[3], rows[1])

    def test_empty_and_truncated_input_rejected(self):
        for language in COMMANDS:
            for payload in [b"", b"\x00" * 7, b"\x00" * 9]:
                with self.subTest(language=language, bytes=len(payload)):
                    process, rows = self.invoke(language, payload, "QUIT\n")
                    self.assertNotEqual(process.returncode, 0)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["status"], "error")

    def test_unknown_operation_rejected(self):
        for language in COMMANDS:
            with self.subTest(language=language):
                process, rows = self.invoke(language, struct.pack("<q", 1), "CHECK\tD01\tlookup\n")
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(rows[-1]["status"], "error")

    def test_malformed_protocol_rejected(self):
        for language in COMMANDS:
            with self.subTest(language=language):
                process, rows = self.invoke(language, struct.pack("<q", 1), "CHECK\n")
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(rows[-1]["status"], "error")


if __name__ == "__main__":
    unittest.main()
