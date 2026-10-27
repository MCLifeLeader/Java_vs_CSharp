"""Statistics use independent process pairs and never fill missing evidence with zeros."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reporting.analysis import leaf_scores, paired_comparison, performance_index, percentile, slo_summary


class AnalysisTests(unittest.TestCase):
    def test_five_process_pairs_required(self):
        self.assertIsNone(paired_comparison([(2, 1)] * 4)["ratio"])

    def test_bootstrap_resamples_whole_pairs_deterministically(self):
        first = paired_comparison([(2, 1), (4, 2), (6, 3), (8, 4), (10, 5)], resamples=1000)
        self.assertEqual(first, paired_comparison([(2, 1), (4, 2), (6, 3), (8, 4), (10, 5)], resamples=1000))
        self.assertAlmostEqual(2, first["ratio"])
        self.assertEqual("csharp", first["advantage"])

    def test_small_consistent_advantage_remains_practically_similar(self):
        self.assertEqual("inconclusive_or_practically_similar", paired_comparison([(1.01, 1)] * 5, resamples=100)["advantage"])

    def test_zero_resource_observation_is_not_scored(self):
        with self.assertRaises(ValueError):
            paired_comparison([(1, 0)] * 5)

    def test_lower_is_better_direction_and_leaf_scores(self):
        self.assertEqual({"csharp": 5, "java": 2.5}, leaf_scores(1, 2, "lower"))
        self.assertEqual("csharp", paired_comparison([(1, 2)] * 5, "lower", resamples=100)["advantage"])

    def test_missing_mandatory_leaf_keeps_full_index_unavailable(self):
        leaf = {"leaf_id": "one", "weight": 50, "csharp": 2, "java": 1, "direction": "higher", "admitted": True}
        result = performance_index([leaf], ["one", "two"], frozen=True)
        self.assertIsNone(result["performance_index"])
        self.assertEqual(["two"], result["missing"])

    def test_unfrozen_weights_keep_full_index_unavailable(self):
        self.assertIsNone(performance_index([], [], frozen=False)["performance_index"])

    def test_full_index_formula_and_overall_eight_percent(self):
        leaf = {"leaf_id": "one", "weight": 100, "csharp": 2, "java": 1, "direction": "higher", "admitted": True}
        result = performance_index([leaf], ["one"], frozen=True)
        self.assertEqual({"csharp": 100, "java": 50}, result["performance_index"])
        self.assertEqual({"csharp": 8, "java": 4}, result["overall_assessment_contribution"])

    def test_unsent_arrivals_fail_slo_without_zero_latency(self):
        observations = [{"arrival": 0, "status": "success", "latency_ms": 1},
                        {"arrival": 1, "status": "unsent", "latency_ms": None}]
        result = slo_summary(observations, 1)
        self.assertFalse(result["slo_satisfied"])
        self.assertEqual(.5, result["error_rate"])
        self.assertEqual(1, result["successful_response_p99_ms"])
        self.assertIsNone(result["p99_9_ms"])


if __name__ == "__main__":
    unittest.main()
