"""Validate correctness-only smoke evidence and report incomplete campaign coverage."""

import hashlib
import json
from pathlib import Path
import re
from datetime import datetime


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "config" / "smoke-result.schema.json"


def enforce_schema(value, schema, path="result"):
    """Enforce only the JSON Schema vocabulary used by the published smoke schema."""
    kinds = {"object": dict, "array": list, "string": str, "integer": int,
             "boolean": bool, "null": type(None)}
    if "type" in schema and type(value) is not kinds[schema["type"]]:
        raise ValueError(f"Schema type mismatch at {path}")
    if "const" in schema and (type(value) is not type(schema["const"]) or value != schema["const"]):
        raise ValueError(f"Schema constant mismatch at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Schema enumeration mismatch at {path}")
    if isinstance(value, dict):
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"Schema required field missing at {path}")
        if len(value) < schema.get("minProperties", 0):
            raise ValueError(f"Schema empty object at {path}")
        for key, item in value.items():
            child = schema.get("properties", {}).get(key)
            if child is None:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise ValueError(f"Schema unknown field at {path}.{key}")
                child = additional if isinstance(additional, dict) else {}
            enforce_schema(item, child, path + "." + key)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"Schema empty array at {path}")
        for index, item in enumerate(value):
            enforce_schema(item, schema.get("items", {}), f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"Schema short string at {path}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"Schema pattern mismatch at {path}")
    if type(value) is int and "minimum" in schema and value < schema["minimum"]:
        raise ValueError(f"Schema numeric minimum at {path}")


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def validate_result(result):
    """Fail closed: these records are never scored or admitted as performance evidence."""
    enforce_schema(result, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    required = {
        "contract_version", "campaign_id", "case_id", "parameter_id", "pair_id",
        "repetition", "language", "engine", "access", "profile", "dataset", "workers",
        "track", "case_hash", "status", "fingerprint", "provenance", "input", "outcome",
        "performance", "admission", "evidence", "result_hash",
    }
    if not isinstance(result, dict) or required - result.keys():
        raise ValueError("Incomplete smoke result identity or field groups")
    if result["contract_version"] != "smoke-1" or result["status"] != "validated":
        raise ValueError("Only validated smoke-1 results are supported")
    if result["language"] not in ("csharp", "java") or result["track"] != "lts":
        raise ValueError("Unexpected language or track")
    for field in ("repetition", "workers"):
        if type(result[field]) is not int or result[field] < 1:
            raise ValueError(f"Invalid {field}")
    for field in ("case_hash", "fingerprint", "result_hash"):
        if not is_hash(result[field]):
            raise ValueError(f"Invalid {field}")
    provenance = result["provenance"]
    try:
        started = datetime.fromisoformat(provenance["utc_start"])
        ended = datetime.fromisoformat(provenance["utc_end"])
        if started.utcoffset() is None or ended.utcoffset() is None or ended < started:
            raise ValueError("Invalid chronology")
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid smoke UTC chronology") from error
    for key in ("source_hash", "host_manifest_hash", "versions_hash", "config_hash"):
        if not is_hash(provenance.get(key)):
            raise ValueError(f"Missing provenance hash: {key}")
    if not provenance.get("command") or not provenance.get("source_commit"):
        raise ValueError("Missing exact command or source commit")
    inputs = result["input"]
    if not is_hash(inputs.get("dataset_hash")) or not inputs.get("seed"):
        raise ValueError("Missing canonical input identity")
    outcome = result["outcome"]
    for key in ("attempts", "successes", "failures"):
        if type(outcome.get(key)) is not int or outcome[key] < 0:
            raise ValueError(f"Invalid outcome count: {key}")
    if outcome["attempts"] != outcome["successes"] + outcome["failures"]:
        raise ValueError("Outcome attempts disappeared")
    if (outcome["correctness"] != "pass" or outcome["attempts"] != 1
            or outcome["successes"] != 1 or outcome["failures"] != 0):
        raise ValueError("Smoke result is not one correct execution")
    if not isinstance(outcome.get("checksum"), str) or len(outcome["checksum"]) != 16:
        raise ValueError("Invalid output checksum")
    try:
        int(outcome["checksum"], 16)
    except ValueError as error:
        raise ValueError("Invalid output checksum") from error
    if type(outcome.get("items")) is not int or outcome["items"] < 1:
        raise ValueError("Invalid input item count")
    if inputs["payload_bytes"] != outcome["items"] * 8:
        raise ValueError("Input bytes and returned item count differ")
    if (result["case_id"], result["parameter_id"]) not in (("C01", "transform"), ("K01", "scan"), ("K01", "sort")):
        raise ValueError("Unsupported smoke operation")
    pair_identity = {key: result[key] for key in ("case_id", "parameter_id", "engine", "access", "profile",
                                                  "dataset", "workers", "repetition", "track")}
    if result["pair_id"] != canonical_hash(pair_identity):
        raise ValueError("Paired identity hash differs")
    performance = result["performance"]
    if (performance.get("measured") is not False or not performance.get("unavailable_reason")
            or any(performance.get(key) is not None for key in
                   ("throughput_ops_s", "cpu_seconds", "peak_rss_bytes", "p99_ms"))):
        raise ValueError("Smoke results cannot contain performance measurements")
    if result["admission"].get("admitted") is not False or not result["admission"].get("reason"):
        raise ValueError("Smoke results cannot be admitted for ranking")
    if not isinstance(result["evidence"], dict) or not result["evidence"]:
        raise ValueError("Missing evidence files")
    for path, digest in result["evidence"].items():
        if not isinstance(path, str) or not is_hash(digest):
            raise ValueError("Invalid evidence inventory")
    unsigned = {key: value for key, value in result.items() if key != "result_hash"}
    if canonical_hash(unsigned) != result["result_hash"]:
        raise ValueError("Smoke result content hash differs")


def is_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def verify_evidence(root, result):
    root = Path(root).resolve()
    for relative, expected_hash in result["evidence"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Evidence file is missing or outside benchmark root")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"Evidence hash differs: {relative}")


def summarize(root, campaign_id):
    root = Path(root).resolve()
    if not campaign_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in campaign_id):
        raise ValueError("Invalid campaign ID")
    campaign = root / "results" / campaign_id
    manifest = json.loads((campaign / "manifest.json").read_text(encoding="utf-8"))
    unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if manifest.get("manifest_hash") != canonical_hash(unsigned_manifest):
        raise ValueError("Campaign manifest content hash differs")
    archived_manifest = json.loads((root / "evidence" / campaign_id / "campaign-manifest.json").read_text(encoding="utf-8"))
    if archived_manifest != manifest:
        raise ValueError("Campaign manifest differs from its original evidence copy")
    if manifest["gaps"] != [row for row in manifest["selected_inventory"] if not row["implemented"]]:
        raise ValueError("Campaign gap inventory differs")
    records = []
    for path in sorted((campaign / "records").glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        validate_result(record)
        verify_evidence(root, record)
        if record["campaign_id"] != campaign_id or record["fingerprint"] != manifest["fingerprint"]:
            raise ValueError("Record belongs to a different campaign fingerprint")
        records.append(record)
    expected = {row["case_hash"] for row in manifest["selected_inventory"] if row["implemented"]}
    expected_rows = {row["case_hash"]: row for row in manifest["selected_inventory"] if row["implemented"]}
    for record in records:
        row = expected_rows.get(record["case_hash"])
        if row is None or any(record[key] != row[key] for key in
                              ("case_id", "parameter_id", "language", "engine", "access", "profile",
                               "dataset", "workers", "repetition", "track")):
            raise ValueError("Record identity differs from selected inventory")
    observed = [row["case_hash"] for row in records]
    if len(observed) != len(set(observed)) or set(observed) - expected:
        raise ValueError("Duplicate or unexpected result records")
    missing = len(expected - set(observed))
    gaps = manifest["gaps"]
    return {
        "campaign_id": campaign_id,
        "stage": "smoke",
        "track": manifest["config"]["track"],
        "status": "complete_selected_smoke" if not missing and not gaps else "partial",
        "scope": "Correctness-only selected subset; not the full performance suite",
        "selected_inventory_rows": len(manifest["selected_inventory"]),
        "validated_results": len(records),
        "missing_implemented_results": missing,
        "not_implemented_or_blocked_rows": len(gaps),
        "authored_case_ids_validated": sorted({record["case_id"] for record in records}),
        "performance_index": None,
        "performance_unavailable_reason": "No timed campaign, pilot, frozen resources or admitted repetitions",
        "code_coverage": None,
        "code_coverage_reason": "No measured code-coverage report",
        "test_counts_are_code_coverage": False,
        "records": [str(path.relative_to(root)).replace("\\", "/")
                    for path in sorted((campaign / "records").glob("*.json"))],
    }
