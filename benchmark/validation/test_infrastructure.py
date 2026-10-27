"""Destructive database operations require controller-created ownership identities."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller.infrastructure import DatabaseEnvironment
from controller.resources import validate_limits


class InfrastructureTests(unittest.TestCase):
    def test_unpinned_image_is_rejected_before_any_process(self):
        environment = DatabaseEnvironment(Path("."), {"infrastructure": {"postgresql": {"image": "postgres:latest"}}}, "postgresql")
        with patch.object(environment, "docker", side_effect=AssertionError("Unexpected Docker process")):
            with self.assertRaisesRegex(ValueError, "immutable"):
                environment.__enter__()

    def test_sqlserver_eula_is_never_implicitly_accepted(self):
        environment = DatabaseEnvironment(Path("."), {"infrastructure": {"sqlserver": {"image": "mssql@sha256:" + "a" * 64}}}, "sqlserver")
        with patch.object(environment, "docker", side_effect=AssertionError("Unexpected Docker process")):
            with self.assertRaisesRegex(ValueError, "EULA"):
                environment.__enter__()

    def test_arbitrary_database_name_cannot_be_dropped(self):
        environment = DatabaseEnvironment(Path("."), {}, "postgresql")
        with patch.object(environment, "_sql", side_effect=AssertionError("Unexpected SQL execution")):
            with self.assertRaisesRegex(ValueError, "not created"):
                environment.drop_database("production")

    def test_forged_owned_prefix_is_not_enough(self):
        environment = DatabaseEnvironment(Path("."), {}, "sqlserver")
        with patch.object(environment, "_sql", side_effect=AssertionError("Unexpected SQL execution")):
            with self.assertRaises(ValueError):
                environment.drop_database("bench_" + "a" * 32)

    def test_equal_process_budget_requires_memory_and_valid_cpu_ids(self):
        with self.assertRaises(ValueError):
            validate_limits({"memory_bytes": 0, "cpu_affinity": [0]})
        with self.assertRaises(ValueError):
            validate_limits({"memory_bytes": 1024 ** 3, "cpu_affinity": [-1]})


if __name__ == "__main__":
    unittest.main()
