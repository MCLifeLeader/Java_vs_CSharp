"""Inventory-derived metric leaves, whole-pair comparisons and fixed-weight views."""

from collections import defaultdict
import json
from pathlib import Path

from controller.inventory import expand_inventory
from reporting.analysis import geometric_mean, paired_comparison, performance_index, positive
from reporting.report import canonical_hash


def endpoint(row):
    return row["parameter_id"].rsplit("-", 1)[0] if row["case_id"].startswith("H") else row["parameter_id"]


def identity_key(row):
    return canonical_hash({**{key: row[key] for key in ("case_id", "engine", "access", "profile", "dataset", "workers", "track")},
                           "parameter_id": endpoint(row)})


METRICS = {
    "throughput": ("throughput_per_second", "higher"), "cpu_per_op": ("cpu_per_work_unit", "lower"),
    "peak_rss": ("peak_rss_bytes", "lower"), "slo_throughput": ("slo_throughput", "higher"),
    "common_rate_p99": ("p99_ms", "lower"), "total_cpu_per_op": ("total_cpu_per_work_unit", "lower"),
    "application_peak_rss": ("peak_rss_bytes", "lower"), "P01_first_response": ("first_correct_seconds", "lower"),
    "P02_idle_rss": ("idle_rss_bytes", "lower"), "P04_runtime_image_size": ("runtime_inclusive_bytes", "lower"),
}


def metric_value(record, metric):
    performance = record.get("performance") or {}
    if metric == "slo_throughput":
        load = record.get("http_load")
        return performance.get("slo_throughput") if load and load["slo"]["slo_satisfied"] else None
    if metric in ("common_rate_p99", "cpu_per_op", "peak_rss") and record["identity"]["case_id"].startswith("H"):
        if not record["identity"]["parameter_id"].endswith("common70"):
            return None
        if metric == "cpu_per_op":
            successes = record["http_load"]["successful"]
            return performance.get("cpu_seconds", 0) / successes if successes else None
    return performance.get(METRICS[metric][0])


def expected_leaves(root, config, weights):
    # Generate the entire designated portfolio even when this campaign selected a
    # named subset. Missing inventory cannot disappear by editing a run manifest.
    rows = expand_inventory(Path(root), config, {"dataset": "S", "repetition": 1})
    templates = {identity_key(row): row for row in rows if row["score_designated"] and row["language"] == "csharp"}
    cases = defaultdict(set)
    variants = defaultdict(set)
    for key, row in templates.items():
        cases[row["case_id"][0]].add(row["case_id"])
        variants[row["case_id"]].add(endpoint(row))
    leaves = []
    for key, row in sorted(templates.items()):
        domain = row["case_id"][0]
        if domain == "P":
            metric = {"P01": "P01_first_response", "P02": "P02_idle_rss", "P04": "P04_runtime_image_size"}.get(row["case_id"])
            metrics = {metric: weights["domains"]["P"]["metrics"][metric]} if metric else {}
            case_fraction = 1
        else:
            metrics = weights["domains"][domain]["metrics"]
            case_fraction = 1 / len(cases[domain])
        cell_fraction = 1
        if row["engine"]:
            cell_fraction = ((.5 if row["engine"] != "sqlite" else 0) if row["case_id"] == "H06"
                             else weights["engines"][row["engine"]] / 100) * weights["access"][row["access"]] / 100
        for metric, metric_weight in metrics.items():
            leaves.append({"leaf_id": key + "/" + metric, "identity_key": key, "identity": row, "metric": metric,
                           "direction": METRICS[metric][1], "domain": domain, "engine": row["engine"], "access": row["access"],
                           "weight": weights["domains"][domain]["weight"] * case_fraction / len(variants[row["case_id"]]) * cell_fraction * metric_weight / 100})
    return leaves


def analyze_campaign(root, manifest, records):
    weights = manifest.get("weights") or json.loads((Path(root) / "config/weights.json").read_text(encoding="utf-8"))
    expected = expected_leaves(root, manifest["config"], weights)
    by_identity = defaultdict(list)
    for record in records:
        by_identity[identity_key(record["identity"])].append(record)
    leaves, comparisons = [], []
    for definition in expected:
        repetitions = defaultdict(dict)
        for record in by_identity[definition["identity_key"]]:
            if record.get("admitted") is not True or not record.get("gates") or not all(record["gates"].values()):
                continue
            value = metric_value(record, definition["metric"])
            if not positive(value):
                continue
            repetition, language = record["identity"]["repetition"], record["identity"]["language"]
            if language in repetitions[repetition]:
                # SLO throughput is the highest actually satisfying measured
                # arrival rate; common-rate latency has exactly one point.
                if definition["metric"] != "slo_throughput":
                    raise ValueError("Duplicate measured value for one paired metric")
                repetitions[repetition][language] = max(repetitions[repetition][language], value)
            else:
                repetitions[repetition][language] = value
        pairs = [(values["csharp"], values["java"]) for _, values in sorted(repetitions.items()) if set(values) == {"csharp", "java"}]
        comparison = paired_comparison(pairs, definition["direction"], manifest["config"]["seed"])
        comparisons.append({**definition, "comparison": comparison})
        required_pairs=30 if definition['identity']['case_id']=='P01' else manifest['config']['repetitions']
        leaves.append({**definition, "csharp": geometric_mean([pair[0] for pair in pairs]) if len(pairs) >= 5 else None,
                       "java": geometric_mean([pair[1] for pair in pairs]) if len(pairs) >= 5 else None, "admitted": len(pairs) >= 5})
        leaves[-1]['admitted']=len(pairs)>=required_pairs
        comparisons[-1]['required_pairs']=required_pairs
    frozen = manifest["stage"] == "full" and weights.get("frozen") is True
    expected_ids = [leaf["leaf_id"] for leaf in expected]
    primary = performance_index(leaves, expected_ids, frozen=frozen)
    views = {}
    for name, predicate in (("sqlserver", lambda leaf: leaf["engine"] == "sqlserver"),
                            ("postgresql", lambda leaf: leaf["engine"] == "postgresql"),
                            ("sqlite", lambda leaf: leaf["engine"] == "sqlite"),
                            ("orm", lambda leaf: leaf["access"] == "orm"),
                            ("raw", lambda leaf: leaf["access"] == "raw")):
        selected = [leaf for leaf in leaves if predicate(leaf)]
        # A named scope also preserves its own mandatory gaps.
        if selected and all(leaf["admitted"] for leaf in selected):
            views[name] = performance_index(selected, [leaf["leaf_id"] for leaf in selected], frozen=frozen, partial_name=name)
        else:
            views[name] = {"status": "unavailable", "performance_index": None, "reason": "Named scope lacks mandatory admitted pairs"}
    sensitivities = {}
    profiles = {"equal_domains": {domain: 10 for domain in weights["domains"] if domain != "L"},
                "database_heavy": {**{domain: item["weight"] * 80 / 82 for domain, item in weights["domains"].items() if domain not in ("D", "L")}, "D": 20},
                "latency_heavy": {**{domain: item["weight"] * 75 / 86 for domain, item in weights["domains"].items() if domain not in ("H", "L")}, "H": 25}}
    for name, domain_weights in profiles.items():
        changed = [{**leaf, "weight": leaf["weight"] * domain_weights[leaf["domain"]] / weights["domains"][leaf["domain"]]["weight"]} for leaf in leaves]
        sensitivities[name] = performance_index(changed, expected_ids, frozen=frozen)
    network = [{**leaf, "weight": leaf["weight"] * (1.25 if leaf["engine"] in ("sqlserver", "postgresql") and leaf["domain"] == "D" else 1)}
               for leaf in leaves if not (leaf["engine"] == "sqlite" and leaf["domain"] == "D")]
    sensitivities["database_sqlite_excluded"] = performance_index(network, [leaf["leaf_id"] for leaf in network], frozen=frozen)
    return {"primary": primary, "comparisons": comparisons, "named_scopes": views, "sensitivities": sensitivities,
            "track": manifest["config"]["track"], "stable_track_policy": "LTS is the explicitly selected campaign; feature-generation track remains separate",
            "aggregate_uncertainty": None, "aggregate_uncertainty_reason": "No preregistered common cross-case block covariance in this controller; per-leaf whole-pair bootstrap is reported"}
