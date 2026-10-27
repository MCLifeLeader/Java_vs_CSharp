"""Contract checks for planned scope, parity cells and honest inventory state."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "controller"))
from inventory import expand_inventory, load_catalog


class InventoryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "config" / "campaign.json").read_text(encoding="utf-8"))

    def test_all_55_authored_cases_are_present_without_duplicates(self):
        catalog = load_catalog(ROOT)
        self.assertEqual(len(catalog), 55)
        self.assertEqual(sum(len(case["parameters"]) for case in catalog), 180)
        counts = {prefix: sum(case["case_id"].startswith(prefix) for case in catalog)
                  for prefix in "DCKMASIHPNL"}
        self.assertEqual(counts, dict(D=8, C=6, K=6, M=5, A=5, S=4, I=4, H=6, P=4, N=2, L=5))

    def test_database_inventory_has_12_cells_and_required_scaling(self):
        rows = expand_inventory(ROOT, self.config, {"case": "D01", "repetition": 1})
        cells = {(row["language"], row["engine"], row["access"]) for row in rows}
        self.assertEqual(len(cells), 12)
        self.assertEqual(len(rows), 28)  # Two network engines x 2 modes x 2 languages x 3 workers, plus SQLite x 4.
        for language, engine, access in cells:
            workers = {row["workers"] for row in rows
                       if (row["language"], row["engine"], row["access"]) == (language, engine, access)}
            self.assertEqual(workers, {1} if engine == "sqlite" else {1, 8, 32})

    def test_all_eight_database_workloads_have_same_12_cells(self):
        rows = expand_inventory(ROOT, self.config, {"workers": 1, "repetition": 1})
        for number in range(1, 9):
            self.assertEqual(sum(row["case_id"] == f"D{number:02}" for row in rows), 12)

    def test_hashes_are_unique_reproducible_and_config_sensitive(self):
        selectors = {"case": "C01,K01", "repetition": 1}
        first = expand_inventory(ROOT, self.config, selectors)
        self.assertEqual(first, expand_inventory(ROOT, dict(reversed(list(self.config.items()))), selectors))
        self.assertEqual(len({row["case_hash"] for row in first}), len(first))
        changed = dict(self.config, seed=self.config["seed"] + 1)
        second = expand_inventory(ROOT, changed, selectors)
        self.assertTrue({row["case_hash"] for row in first}.isdisjoint(row["case_hash"] for row in second))

    def test_pairs_match_only_language_and_repeat_ids_differ(self):
        rows = expand_inventory(ROOT, self.config, {"case": "C01"})
        for repetition in range(1, 6):
            pair = [row for row in rows if row["repetition"] == repetition]
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0]["pair_id"], pair[1]["pair_id"])
        self.assertEqual(len({row["pair_id"] for row in rows}), 5)

    def test_full_implementation_is_distinct_from_execution_and_admission(self):
        rows = expand_inventory(ROOT, self.config, {"dataset": "smoke", "repetition": 1})
        self.assertEqual({row["case_id"] for row in rows if row["implemented"]},
                         {case["case_id"] for case in load_catalog(ROOT)})
        self.assertTrue(all(row["status"] == "implemented" and row["verification_scope"] for row in rows))
        self.assertTrue(all(not row["scored"] and not row["measured"] and not row["admitted"] for row in rows))
        medium = expand_inventory(ROOT, self.config, {"dataset": "M", "repetition": 1})
        self.assertTrue(all(not row["implemented"] for row in medium))

    def test_requested_track_is_explicitly_deferred(self):
        rows = expand_inventory(ROOT, dict(self.config, track="requested"), {"case": "C01"})
        self.assertTrue(all(not row["implemented"] and "deferred" in row["reason"] for row in rows))

    def test_startup_and_build_have_30_and_10_independent_repetitions(self):
        for case_id, parameter, expected in [("P01", "H02", 30), ("L05", "clean", 10)]:
            rows = expand_inventory(ROOT, self.config, {"case": case_id, "language": "csharp"})
            self.assertEqual({row["repetition"] for row in rows if row["parameter_id"] == parameter},
                             set(range(1, expected + 1)))

    def test_http_checkout_sqlite_is_separate_supplemental_scope(self):
        rows = expand_inventory(ROOT, self.config, {"case": "H06", "repetition": 1})
        sqlite = [row for row in rows if row["engine"] == "sqlite"]
        network = [row for row in rows if row["engine"] in {"sqlserver", "postgresql"}]
        self.assertTrue(sqlite and network)
        self.assertTrue(all(not row["required"] and not row["score_designated"] and row["workers"] == 1 for row in sqlite))
        self.assertTrue(all(row["required"] and row["unresolved_parameters"] for row in network))

    def test_http_rate_grid_and_unknown_pilot_workers_stay_visible(self):
        rows = expand_inventory(ROOT, self.config, {"case": "H01", "repetition": 1, "language": "csharp"})
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row["workers"] is None and row["unresolved_parameters"] for row in rows))

    def test_a04_idiomatic_is_required_but_never_score_designated(self):
        rows = expand_inventory(ROOT, self.config, {"case": "A04", "profile": "idiomatic"})
        self.assertTrue(rows)
        self.assertTrue(all(row["required"] and not row["score_designated"] for row in rows))

    def test_diagnostics_never_enter_performance_score(self):
        rows = expand_inventory(ROOT, self.config, {"case": "A05,P03,L01,L02,L03,L04,L05"})
        self.assertTrue(all(not row["score_designated"] for row in rows))

    def test_packaging_selects_one_matched_deployment_kind(self):
        rows = expand_inventory(ROOT, self.config, {"case": "P04", "repetition": 1})
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["parameter_id"] for row in rows}, {"packaged-service"})
        case = next(case for case in load_catalog(ROOT) if case["case_id"] == "P04")
        self.assertEqual(case["deployment_kind"], "executable")
        self.assertTrue(all(row["unresolved_parameters"] for row in rows))

    def test_invalid_selector_fails_instead_of_silent_empty_inventory(self):
        for selector in [{"case": "D09"}, {"engine": "h2"}, {"language": "python"},
                         {"access": "micro-orm"}, {"workers": 0}, {"repetition": "-1"},
                         {"dataset": "unknown"}, {"profile": "aot"}, {"typo": "C01"}]:
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                expand_inventory(ROOT, self.config, selector)

    def test_non_database_cases_do_not_expand_for_engine_or_access(self):
        self.assertEqual(expand_inventory(ROOT, self.config, {"case": "C01", "engine": "sqlite"}), [])
        rows = expand_inventory(ROOT, self.config, {"case": "C01", "repetition": 1})
        self.assertTrue(all(row["engine"] is None and row["access"] is None for row in rows))

    def test_sqlite_multiple_workers_are_rejected_for_mandatory_baseline(self):
        with self.assertRaises(ValueError):
            expand_inventory(ROOT, dict(self.config, sqlite_workers=[1, 8]), {"case": "D01"})

    def test_invalid_configuration_is_rejected(self):
        for change in [dict(languages=["java", "java"]), dict(engines=["h2"]), dict(repetitions=0),
                       dict(track="mystery"), dict(network_workers=[1, 1]), dict(network_workers=[True])]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                expand_inventory(ROOT, dict(self.config, **change))

    def test_missing_case_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            document = json.loads((ROOT / "config" / "cases.json").read_text(encoding="utf-8"))
            document["cases"].pop()
            (root / "config" / "cases.json").write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_catalog(root)


if __name__ == "__main__":
    unittest.main()
