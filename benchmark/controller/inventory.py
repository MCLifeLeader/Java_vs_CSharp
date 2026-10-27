"""Expand the authoritative planned catalog without executing or scoring work."""

import hashlib
import json
from pathlib import Path
import re


EXPECTED_IDS = {
    f"{domain}{number:02}"
    for domain, count in {"D": 8, "C": 6, "K": 6, "M": 5, "A": 5, "S": 4,
                          "I": 4, "H": 6, "P": 4, "N": 2, "L": 5}.items()
    for number in range(1, count + 1)
}
LANGUAGES = {"csharp", "java"}
ENGINES = {"sqlserver", "postgresql", "sqlite"}
ACCESS_MODES = {"orm", "raw"}
PROFILES = {"controlled", "idiomatic"}
DATASETS = {"S", "M", "smoke"}
TRACKS = {"lts", "requested"}
SELECTOR_FIELDS = {"case": "case_id", "language": "language", "engine": "engine",
                   "access": "access", "profile": "profile", "dataset": "dataset",
                   "workers": "workers", "repetition": "repetition"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _choices(values, allowed, name):
    if not isinstance(values, list) or not values or any(v not in allowed for v in values):
        raise ValueError(f"Invalid {name}: expected a nonempty list from {sorted(allowed)}")
    if len(set(values)) != len(values):
        raise ValueError(f"Duplicate {name}")
    return values


def load_catalog(root: Path) -> list[dict]:
    """Read all 55 authored definitions from a benchmark directory."""
    document = json.loads((Path(root) / "config" / "cases.json").read_text(encoding="utf-8"))
    cases = document["cases"]
    ids = [case["case_id"] for case in cases]
    if len(ids) != len(set(ids)) or set(ids) != EXPECTED_IDS:
        raise ValueError("Catalog must contain each of the 55 required case IDs exactly once")
    for case in cases:
        parameters = case["parameters"]
        parameter_ids = [p["parameter_id"] for p in parameters]
        if not parameter_ids or len(parameter_ids) != len(set(parameter_ids)):
            raise ValueError(f"Missing or duplicate parameter IDs in {case['case_id']}")
        if any(not re.fullmatch(r"[A-Za-z0-9.-]+", p) for p in parameter_ids):
            raise ValueError(f"Invalid parameter ID in {case['case_id']}")
        _choices(case["profiles"], PROFILES, "profiles")
    return cases


def _selectors(selectors):
    normalized = {}
    allowed_values = {"case": EXPECTED_IDS, "language": LANGUAGES, "engine": ENGINES,
                      "access": ACCESS_MODES, "profile": PROFILES, "dataset": DATASETS}
    for key, value in (selectors or {}).items():
        if key not in SELECTOR_FIELDS:
            raise ValueError(f"Unknown selector: {key}")
        if value is None:
            continue
        parts = str(value).split(",") if not isinstance(value, (list, tuple, set)) else list(value)
        if key in ("workers", "repetition"):
            try:
                parts = [_positive_int(int(part), key) for part in parts]
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid positive-integer selector: {key}") from error
        elif any(part not in allowed_values[key] for part in parts):
            raise ValueError(f"Invalid selector value for {key}: {value}")
        normalized[key] = set(parts)
    return normalized


def _cells(case, parameter, engines, accesses):
    has_database = case.get("database") == "all" or (
        case.get("database") == "parameter" and parameter.get("database") is True)
    if has_database:
        return [(engine, access) for engine in engines for access in accesses]
    return [(None, None)]


def _workers(case, engine, config):
    if case["workers"] == "database":
        name = "sqlite_workers" if engine == "sqlite" else "network_workers"
        workers = config.get(name, [1] if engine == "sqlite" else [1, 8, 32])
        if not isinstance(workers, list) or not workers:
            raise ValueError(f"{name} must be a nonempty list")
        for value in workers:
            _positive_int(value, name)
        if len(workers) != len(set(workers)):
            raise ValueError(f"Duplicate {name}")
        if engine == "sqlite" and workers != [1]:
            raise ValueError("The mandatory SQLite profile has exactly one worker")
        return workers
    # Embedded HTTP uses the mandatory single-worker resource boundary too.
    return [1] if engine == "sqlite" else case["workers"]


def _designated(case, parameter, row):
    if row["dataset"] != "S" or row["profile"] != "controlled" or not case.get("scored", True):
        return False
    if case.get("sqlite_supplemental") and row["engine"] == "sqlite":
        return False
    if row["case_id"] == "P01" and parameter["parameter_id"] != "H02":
        return False
    return (row["workers"] in case.get("scored_workers", [row["workers"]])
            and parameter["parameter_id"] in case.get("scored_parameters", [parameter["parameter_id"]]))


def expand_inventory(root: Path, config: dict, selectors: dict | None = None) -> list[dict]:
    """One row per planned case/parameter/language/profile/cell/worker/repetition.

    This is a planned inventory. Null worker counts and unresolved parameter lists
    expose decisions that still need a pilot. ``scored`` is always false because
    enumerating a row is not a measurement; ``score_designated`` labels the
    eventual designated configuration. No result is admitted by this module.
    """
    filters = _selectors(selectors)
    languages = _choices(config.get("languages", ["csharp", "java"]), LANGUAGES, "languages")
    engines = _choices(config.get("engines", ["sqlserver", "postgresql", "sqlite"]), ENGINES, "engines")
    accesses = _choices(config.get("access_modes", ["orm", "raw"]), ACCESS_MODES, "access modes")
    track = config.get("track", "lts")
    dataset = config.get("dataset", "S")
    if track not in TRACKS or dataset not in DATASETS:
        raise ValueError("Unknown track or dataset")
    repetitions = _positive_int(config.get("repetitions", 5), "repetitions")
    datasets = sorted(filters.get("dataset", {dataset}))
    rows = []
    for case in load_catalog(root):
        for parameter in case["parameters"]:
            for engine, access in _cells(case, parameter, engines, accesses):
                for profile in case["profiles"]:
                    for workers in _workers(case, engine, config):
                        for selected_dataset in datasets:
                            for repetition in range(1, case.get("repetitions", repetitions) + 1):
                                for language in languages:
                                    row = {"case_id": case["case_id"], "parameter_id": parameter["parameter_id"],
                                           "language": language, "engine": engine, "access": access,
                                           "profile": profile, "dataset": selected_dataset, "workers": workers,
                                           "repetition": repetition, "track": track}
                                    if any(row[SELECTOR_FIELDS[key]] not in values
                                           for key, values in filters.items()):
                                        continue
                                    implemented = (case["implementation"] == "implemented" and track == "lts"
                                                   and selected_dataset in {"S", "smoke"})
                                    reason = case["reason"] if track == "lts" else "Requested C# 15/Java 27 track deferred by user; runtime builds are not validated."
                                    row["case_hash"] = _digest({"identity": row, "definition": case,
                                                                "parameter": parameter, "config": config})
                                    pair_identity = {key: value for key, value in row.items()
                                                     if key not in {"language", "case_hash"}}
                                    row.update(implemented=implemented, reason=reason,
                                               status="implemented" if implemented else "not_implemented",
                                               verification_scope=case.get("verification_scope", "No execution evidence attached"),
                                               scored=False, measured=False, admitted=False,
                                               score_designated=_designated(case, parameter, row),
                                               required=not (case.get("sqlite_supplemental") and engine == "sqlite"),
                                               unresolved_parameters=list(case["unresolved_parameters"]),
                                               pair_id=_digest({"identity": pair_identity, "config": config})[:24])
                                    rows.append(row)
    return rows
