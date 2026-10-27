"""Whole-pair statistics and completeness-gated best-relative performance scores."""

from collections import defaultdict
import math
import random
import statistics


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def percentile(values, probability):
    if not values:
        return None
    if not 0 <= probability <= 1:
        raise ValueError("Percentile probability is outside [0,1]")
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    low = math.floor(index)
    return ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low)


def geometric_mean(values):
    if not values or any(not positive(value) for value in values):
        raise ValueError("Geometric mean requires finite positive values")
    return math.exp(statistics.mean(math.log(value) for value in values))


def paired_comparison(pairs, direction="higher", seed=20261025, resamples=10000):
    """pairs are (C#, Java) process-repetition estimates, never per-request samples."""
    if direction not in ("higher", "lower") or resamples < 1:
        raise ValueError("Invalid metric direction or bootstrap count")
    if len(pairs) < 5:
        return {"status": "unavailable", "reason": "At least five independent paired process repetitions required",
                "pairs": len(pairs), "ratio": None, "confidence_interval_95": None}
    if any(len(pair) != 2 or not all(positive(value) for value in pair) for pair in pairs):
        raise ValueError("Unusable paired metric; zero/missing/resource failures need diagnosis")
    ratios = [csharp / java if direction == "higher" else java / csharp for csharp, java in pairs]
    estimate = geometric_mean(ratios)
    rng = random.Random(seed)
    bootstrap = [geometric_mean([ratios[rng.randrange(len(ratios))] for _ in ratios]) for _ in range(resamples)]
    interval = [percentile(bootstrap, .025), percentile(bootstrap, .975)]
    advantage = ("csharp" if interval[0] > 1 and estimate >= 1.05 else
                 "java" if interval[1] < 1 and estimate <= 1 / 1.05 else "inconclusive_or_practically_similar")
    return {"status": "exploratory", "pairs": len(pairs), "paired_ratios": ratios, "ratio": estimate,
            "confidence_interval_95": interval, "bootstrap_seed": seed, "bootstrap_resamples": resamples,
            "independent_unit": "Whole paired process repetition", "advantage": advantage,
            "multiple_comparisons": "Unadjusted exploratory interval; no family-wide significance claim",
            "csharp": {"median": statistics.median(pair[0] for pair in pairs),
                       "range": [min(pair[0] for pair in pairs), max(pair[0] for pair in pairs)]},
            "java": {"median": statistics.median(pair[1] for pair in pairs),
                     "range": [min(pair[1] for pair in pairs), max(pair[1] for pair in pairs)]}}


def leaf_scores(csharp, java, direction):
    if not positive(csharp) or not positive(java):
        raise ValueError("Scores require measured finite positive metrics")
    if direction == "higher":
        best = max(csharp, java)
        return {"csharp": 5 * csharp / best, "java": 5 * java / best}
    if direction == "lower":
        best = min(csharp, java)
        return {"csharp": 5 * best / csharp, "java": 5 * best / java}
    raise ValueError("Unknown metric direction")


def performance_index(leaves, expected_leaf_ids, *, frozen=False, partial_name=None):
    """No silent redistribution. Each leaf declares its fixed fraction of 100 points.

    A leaf is {leaf_id,weight,csharp,java,direction,admitted}. All mandatory leaf
    IDs must exist with admitted measurements for a full index. A named partial
    index exposes its original selected weight and explicitly normalizes only
    that named scope; its output can never be labeled the full index.
    """
    if not frozen:
        return {"status": "unavailable", "performance_index": None, "reason": "Campaign weights and run manifest are not frozen"}
    if len({leaf["leaf_id"] for leaf in leaves}) != len(leaves):
        raise ValueError("Duplicate metric leaf")
    expected = set(expected_leaf_ids)
    admitted = {leaf["leaf_id"]: leaf for leaf in leaves if leaf.get("admitted") is True}
    missing = sorted(expected - admitted.keys())
    unexpected = sorted(admitted.keys() - expected)
    if unexpected:
        raise ValueError("Measurements contain unexpected metric leaves")
    if missing and not partial_name:
        return {"status": "unavailable", "performance_index": None,
                "reason": "Mandatory measured metric leaves are missing or not admitted", "missing": missing}
    selected = [admitted[key] for key in sorted(admitted)]
    if not selected or any(not positive(leaf.get("weight")) for leaf in selected):
        raise ValueError("Metric leaf weights must be finite positive values")
    weight = sum(leaf["weight"] for leaf in selected)
    if not partial_name and not math.isclose(weight, 100, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Full index leaf weights must sum to exactly 100")
    totals = {"csharp": 0.0, "java": 0.0}
    for leaf in selected:
        scores = leaf_scores(leaf["csharp"], leaf["java"], leaf["direction"])
        for language in totals:
            totals[language] += scores[language] * leaf["weight"]
    return {"status": "named_partial" if partial_name else "complete",
            "name": partial_name, "performance_index": {language: 20 * total / weight for language, total in totals.items()},
            "original_covered_weight": weight, "missing": missing,
            "overall_assessment_contribution": None if partial_name else {language: 8 * (20 * total / weight) / 100 for language, total in totals.items()},
            "aggregate_uncertainty": "Unavailable until actual cross-metric/cross-case block covariance is retained and bootstrapped"}


def slo_summary(observations, offered_seconds, *, p99_limit_ms=100, error_limit=.001):
    if offered_seconds <= 0 or not observations:
        raise ValueError("SLO needs scheduled work and a positive offered window")
    if len({row["arrival"] for row in observations}) != len(observations):
        raise ValueError("Duplicate scheduled arrival")
    successful = [row for row in observations if row["status"] == "success"]
    latencies = [row["latency_ms"] for row in successful]
    if any(not positive(value) for value in latencies):
        raise ValueError("Invalid successful response latency")
    errors = len(observations) - len(successful)
    p99 = percentile(latencies, .99)
    return {"scheduled": len(observations), "successes": len(successful), "errors": errors,
            "error_rate": errors / len(observations), "successful_response_p99_ms": p99,
            "successful_response_p50_ms": percentile(latencies, .5),
            "successful_response_p95_ms": percentile(latencies, .95),
            "max_successful_response_ms": max(latencies) if latencies else None,
            "slo_satisfied": p99 is not None and p99 <= p99_limit_ms and errors / len(observations) <= error_limit,
            "successful_requests_per_second": len(successful) / offered_seconds,
            "all_attempt_latencies": "Raw observations retain failed completions; unsent latency is unavailable, never zero",
            "p99_9_ms": percentile(latencies, .999) if len(latencies) >= 100000 else None,
            "p99_9_reason": "Tail estimate has substantial run-level uncertainty" if len(latencies) >= 100000 else "Fewer than100000 valid observations"}
