"""Execute and verify native five-fork microbenchmarks as corroborating evidence."""

import json
import math
import os
from pathlib import Path
import subprocess
import shutil
import sys

from reporting.report import canonical_hash


def parse_jmh(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) != 1:
        raise ValueError("Expected exactly one selected JMH scenario")
    row = rows[0]
    if row.get("forks", 0) < 5 or row.get("measurementIterations", 0) < 1:
        raise ValueError("JMH evidence lacks five independent forks")
    metric = row["primaryMetric"]
    score, unit = metric["score"], metric["scoreUnit"]
    factors = {"ns/op": 1e-9, "us/op": 1e-6, "ms/op": .001, "s/op": 1}
    seconds = score * factors[unit] if unit in factors else 1 / score if unit in ("ops/s", "op/s") else None
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("JMH emitted an unusable operation duration")
    return {"seconds_per_iteration": seconds, "forks": row["forks"], "raw": row,
            "boundary": "Native JMH measured invocation; prepared invocation setup excluded"}


def parse_bdn(path):
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = document.get("Benchmarks", [])
    if len(rows) != 1:
        raise ValueError("Expected exactly one selected BenchmarkDotNet scenario")
    row = rows[0]
    measurements = row.get("Measurements", [])
    launches = {item["LaunchIndex"] for item in measurements if item.get("IterationMode") == "Workload"
                and item.get("IterationStage") == "Actual"}
    if len(launches) < 5:
        raise ValueError("BenchmarkDotNet evidence lacks five independent launches")
    seconds = row["Statistics"]["Mean"] / 1e9
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("BenchmarkDotNet emitted an unusable operation duration")
    return {"seconds_per_iteration": seconds, "forks": len(launches), "raw": row,
            "boundary": "Native BenchmarkDotNet invocation; prepared iteration setup excluded"}


def run_harness(root, data, row, fingerprint, evidence, *, limits=None, timeout=1800):
    from controller.benchmarkctl import atomic_json, file_hash
    from controller.suite import suite_command, worker_environment
    from controller.resources import ProcessBudget
    root, evidence = Path(root), Path(evidence)
    identity = {key: row[key] for key in ("case_id", "parameter_id", "language", "dataset", "profile", "workers")}
    key = canonical_hash({"identity": identity, "fingerprint": fingerprint})
    output = evidence / "harness" / key
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "evidence.json"
    if checkpoint.is_file():
        stored = json.loads(checkpoint.read_text(encoding="utf-8"))
        unsigned = {key: value for key, value in stored.items() if key != "evidence_hash"}
        if stored.get("evidence_hash") != canonical_hash(unsigned) or stored.get("fingerprint") != fingerprint:
            raise ValueError("Native harness evidence content/fingerprint changed")
        if any(file_hash(output / name) != digest for name, digest in stored["files"].items()):
            raise ValueError("Native harness raw output changed")
        return stored
    if row["dataset"] != "S" or row["profile"] != "controlled":
        raise ValueError("Native harness baseline requires controlled S fixtures")
    environment = worker_environment(root)
    if row["language"] == "java":
        target = output / "jmh.json"
        command = [sys.executable, str(root / "java/suite/run_jmh.py"), "--data", str(data), "--case", row["case_id"],
                   "--parameter", row["parameter_id"], "--workers", str(row["workers"] or 1), "--output", str(target)]
    else:
        sdk_host = shutil.which("dotnet")
        if not sdk_host:
            raise ValueError("The pinned .NET SDK host must be available for BenchmarkDotNet builds")
        command = suite_command(root, data, row)[:2] + ["--harness", "--filter", "*", "--exporters", "json", "--artifacts", str(output), "--cli", sdk_host]
        environment.update(BENCH_DATA=str(data), BENCH_CASE=row["case_id"], BENCH_PARAMETER=row["parameter_id"])
    budget = None
    # BDN searches below its working directory for the benchmark project. Keep
    # disposable source staging copies outside that search boundary.
    cwd = root / "dotnet/Suite" if row["language"] == "csharp" else root
    process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    try:
        if limits is not None:
            budget = ProcessBudget(process.pid, limits)
        stdout, stderr = process.communicate(timeout=timeout)
        (output / "stdout.log").write_text(stdout, encoding="utf-8")
        (output / "stderr.log").write_text(stderr, encoding="utf-8")
        if process.returncode != 0:
            raise ValueError("Native microbenchmark harness failed; inspect its archived stdout/stderr")
        if row["language"] == "java":
            parsed = parse_jmh(target)
        else:
            candidates = sorted(output.rglob("*-report-full.json")) or sorted(output.rglob("*-report.json"))
            if len(candidates) != 1:
                raise ValueError("BenchmarkDotNet did not export exactly one full JSON report")
            parsed = parse_bdn(candidates[0])
        stored = {"identity": identity, "fingerprint": fingerprint, "command": command, "parsed": parsed,
                  "resource_budget": budget.evidence if budget else None,
                  "files": {str(path.relative_to(output)): file_hash(path) for path in output.rglob("*") if path.is_file()}}
        stored["evidence_hash"] = canonical_hash(stored)
        atomic_json(checkpoint, stored)
        return stored
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if budget:
            budget.close()


def corroborates(batch_seconds_per_iteration, native, tolerance=.25):
    if not 0 < tolerance < 1 or batch_seconds_per_iteration <= 0:
        raise ValueError("Invalid preregistered native harness agreement tolerance")
    ratio = batch_seconds_per_iteration / native["parsed"]["seconds_per_iteration"]
    return {"agrees": abs(ratio - 1) <= tolerance, "external_over_native_duration_ratio": ratio,
            "relative_tolerance": tolerance, "reason": "Same scenario, fixture, source and environment; absolute boundaries remain disclosed"}
