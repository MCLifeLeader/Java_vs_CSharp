"""Benchmark foundation controller: explicit inventory, correctness evidence and safe planning."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
if __name__ == "__main__":
    # Keep exception and helper identities identical when domain modules import
    # this executable entry point during a CLI run.
    sys.modules.setdefault("controller.benchmarkctl", sys.modules[__name__])

from controller.inventory import expand_inventory, load_catalog
from data.generate import generate
from reporting.report import canonical_hash, summarize, validate_result, verify_evidence
from validation.oracle import expected


class Blocked(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_command(command, cwd=ROOT, timeout=60, stdin=None, environment=None):
    try:
        result = subprocess.run([str(part) for part in command], cwd=cwd, input=stdin,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, shell=False, env=environment)
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise Blocked(f"Command unavailable or exceeded {timeout}s: {Path(str(command[0])).name}") from error
    return {"command": [str(part) for part in command], "cwd": str(cwd),
            "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def require_success(record):
    if record["exit_code"] != 0:
        raise Blocked(f"Command failed ({record['exit_code']}): {' '.join(record['command'])}\n"
                      + record["stdout"] + record["stderr"])
    return record


def source_identity():
    commit = require_success(run_command(["git", "rev-parse", "HEAD"], REPO))["stdout"].strip()
    paths = require_success(run_command(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], REPO))["stdout"].split("\0")
    ignored_parts = {".git", ".work", "__pycache__", "bin", "obj", "build", "target"}
    files = {}
    for relative in sorted(set(paths)):
        if not relative:
            continue
        path = Path(relative)
        if ignored_parts.intersection(path.parts) or path.parts[:2] in (("benchmark", "evidence"), ("benchmark", "results")):
            continue
        absolute = REPO / path
        files[relative] = file_hash(absolute) if absolute.is_file() else "deleted"
    return {"commit": commit, "content_hash": canonical_hash(files), "files": files,
            "dirty": bool(require_success(run_command(["git", "status", "--porcelain"], REPO))["stdout"].strip())}


def resolve_config(value):
    path = Path(value)
    if not path.is_absolute() and not path.is_file():
        path = ROOT / path
    if not path.is_file():
        raise ValueError(f"Configuration does not exist: {value}")
    config = read_json(path)
    if config.get("track") != "lts":
        raise Blocked("Only the user-selected lts track has workers; requested track is deferred")
    for field in ("seed", "repetitions", "smoke_items", "process_timeout_seconds"):
        if type(config.get(field)) is not int or config[field] < 1:
            raise ValueError(f"Configuration {field} must be a positive integer")
    if config["smoke_items"] != 4096:
        raise ValueError("smoke_items must match the frozen 4096-item generator profile")
    if config["repetitions"] < 5:
        raise ValueError("Campaign inventory requires at least five repetitions")
    for field, minimum in (("warmup_seconds", 60), ("measurement_seconds", 120), ("mutation_operations", 1)):
        if type(config.get(field)) is not int or config[field] < minimum:
            raise ValueError(f"Configuration {field} must be an integer >= {minimum}")
    for field in ("network_workers", "sqlite_workers"):
        if (not isinstance(config.get(field), list) or not config[field]
                or any(type(value) is not int or value < 1 for value in config[field])
                or len(set(config[field])) != len(config[field])):
            raise ValueError(f"Configuration {field} must contain unique positive integers")
    if config["sqlite_workers"] != [1]:
        raise ValueError("Mandatory SQLite profile requires exactly one worker")
    for field, allowed in (("languages", {"csharp", "java"}),
                           ("engines", {"sqlserver", "postgresql", "sqlite"}),
                           ("access_modes", {"orm", "raw"})):
        if (not isinstance(config.get(field), list)
                or any(not isinstance(value, str) for value in config[field])
                or set(config[field]) != allowed or len(config[field]) != len(allowed)):
            raise ValueError(f"Configuration {field} must preserve the complete baseline set; use selectors for subsets")
    if not isinstance(config.get("full_campaign_authorized"), bool):
        raise ValueError("Missing full campaign execution policy")
    for field in ('http_trace_operations','database_read_batch_operations','http_pilot_seconds','database_pilot_warmup_seconds','generator_calibration_seconds','harness_timeout_seconds','prepare_memory_bytes','http_drain_timeout_seconds'):
        if field in config and (type(config[field]) is not int or config[field]<1):
            raise ValueError('Configuration '+field+' must be a positive integer')
    if 'harness_agreement_tolerance' in config and (type(config['harness_agreement_tolerance']) not in (int,float) or not 0<config['harness_agreement_tolerance']<1):
        raise ValueError('Native harness agreement tolerance must be between zero and one')
    if 'http_pilot_rates' in config and (not isinstance(config['http_pilot_rates'],list) or not config['http_pilot_rates'] or any(type(rate) not in (int,float) or rate<=0 for rate in config['http_pilot_rates']) or config['http_pilot_rates']!=sorted(set(config['http_pilot_rates']))):
        raise ValueError('HTTP pilot rates must be positive, unique and strictly increasing')
    if config.get('process_limits') is not None:
        from controller.resources import validate_limits
        validate_limits(config['process_limits'])
    return config


def selectors_from(args):
    return {key: getattr(args, key, None) for key in
            ("language", "engine", "access", "case", "dataset", "profile", "workers", "repetition")
            if getattr(args, key, None) is not None}


def inventory(root, config, selectors):
    rows = expand_inventory(root, config, selectors)
    if not rows:
        raise ValueError("Selectors matched no inventory rows; no execution performed")
    return rows


def preflight(config):
    lock = read_json(ROOT / "config" / "versions.lock.json")
    selected = lock["lts"]
    checks, observations = [], {}
    commands = {
        "dotnet_sdk": (["dotnet", "--version"], ROOT / "dotnet"),
        "dotnet_runtimes": (["dotnet", "--list-runtimes"], ROOT / "dotnet"),
        "java": (["java", "--version"], ROOT),
        "javac": (["javac", "-version"], ROOT),
    }
    for name, (command, cwd) in commands.items():
        try:
            record = run_command(command, cwd, config["process_timeout_seconds"])
            observations[name] = record
        except Blocked as error:
            checks.append({"name": name, "pass": False, "reason": str(error)})
    def check(name, success, reason):
        checks.append({"name": name, "pass": bool(success), "reason": reason})
    def output(name):
        record = observations.get(name, {})
        return record.get("stdout", "") + record.get("stderr", "") if record.get("exit_code") == 0 else ""
    check("pinned_dotnet_sdk", output("dotnet_sdk").strip() == selected["dotnet_sdk"],
          f"Require SDK {selected['dotnet_sdk']} through dotnet/global.json")
    check("pinned_dotnet_runtime", bool(re.search(r"^Microsoft.NETCore.App " + re.escape(selected["dotnet_runtime"]) + r" ",
                                                  output("dotnet_runtimes"), re.MULTILINE)),
          f"Require runtime {selected['dotnet_runtime']}")
    check("pinned_java_runtime", selected["jdk_runtime"] in output("java") and selected["jdk_vendor"].lower() in output("java").lower(),
          f"Require {selected['jdk_vendor']} runtime {selected['jdk_runtime']}")
    check("pinned_javac", output("javac").strip() == "javac " + selected["jdk_runtime"].split("+")[0],
          "Compiler must match the selected Java runtime release")
    check("python_version", platform.python_version() == selected["python"], "Require locked controller Python version")
    check("lts_track", config["track"] == "lts", "User selected .NET 10 LTS and Java 25 LTS")
    host = {"system": platform.system(), "release": platform.release(), "version": platform.version(),
            "machine": platform.machine(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
            "python": platform.python_version(), "runtime_observations": observations,
            "resource_limits":config.get('process_limits'),"resource_limits_reason":"Applied and verified separately on each measured process",
            "runtime_environment_sha256":{name:hashlib.sha256(value.encode()).hexdigest() for name,value in sorted(os.environ.items())
                                          if name.startswith(('DOTNET_','COMPlus_')) or name in ('JAVA_HOME','JAVA_TOOL_OPTIONS','JDK_JAVA_OPTIONS','_JAVA_OPTIONS')},
            "prepare_memory_bytes":config.get('prepare_memory_bytes',256*1024*1024)}
    tools = {}
    for tool in ("dotnet", "java", "javac"):
        location = shutil.which(tool)
        tools[tool] = {"path": location, "sha256": file_hash(location) if location else None}
    host["tools"] = tools
    return {"status": "ready_for_smoke" if all(item["pass"] for item in checks) else "blocked",
            "checks": checks, "host": host, "host_manifest_hash": canonical_hash(host),
            "database_connections_attempted": False, "sqlserver_eula_accepted": config["sqlserver_eula_accepted"],
            "performance_ready":False,"performance_reason":"Full admission requires frozen pilot, resource and compatibility evidence"}


def build_workers(config):
    timeout = config["process_timeout_seconds"]
    commands = [
        (["dotnet", "restore", "Worker.csproj", "--locked-mode"], ROOT / "dotnet"),
        (["dotnet", "build", "Worker.csproj", "--configuration", "Release", "--no-restore"], ROOT / "dotnet"),
        (["javac", "--release", "25", "-d", str(ROOT / "java" / "build"),
          str(ROOT / "java" / "src" / "BenchmarkWorker.java")], ROOT),
    ]
    records = []
    for command, cwd in commands:
        record = run_command(command, cwd, timeout)
        records.append(record)
        require_success(record)
    return records


def worker_command(language, payload):
    if language == "csharp":
        return ["dotnet", str(ROOT / "dotnet" / "bin" / "Release" / "net10.0" / "Worker.dll"), str(payload)]
    if language == "java":
        return ["java", "-cp", str(ROOT / "java" / "build"), "BenchmarkWorker", str(payload)]
    raise ValueError("Unknown language")


def check_worker(language, payload, checks, timeout):
    command_input = "".join(f"CHECK\t{case}\t{parameter}\n" for case, parameter in checks) + "QUIT\n"
    record = run_command(worker_command(language, payload), ROOT, timeout, command_input)
    require_success(record)
    try:
        messages = [json.loads(line) for line in record["stdout"].splitlines()]
    except json.JSONDecodeError as error:
        raise ValueError("Worker emitted non-JSON output") from error
    if (len(messages) != len(checks) + 1 or messages[0].get("status") != "ready"
            or messages[0].get("language") != language or messages[0].get("track") != "lts"
            or messages[0].get("protocol") != 1 or messages[0].get("measured") is not False):
        raise ValueError("Worker protocol readiness/response count differs")
    raw = Path(payload).read_bytes()
    values = [item[0] for item in struct.iter_unpack("<q", raw)]
    locked = read_json(ROOT / "config" / "versions.lock.json")["lts"]
    runtime = ".NET " + locked["dotnet_runtime"] if language == "csharp" else locked["jdk_runtime"]
    if messages[0].get("runtime") != runtime or messages[0].get("items") != len(values):
        raise ValueError("Worker runtime or loaded input count differs from the frozen contract")
    for message, (case, parameter) in zip(messages[1:], checks, strict=True):
        if message != expected(values, case, parameter):
            raise ValueError(f"Oracle mismatch: {language}/{case}/{parameter}")
    return record, messages[1:]


def semantic_validation(config):
    work = ROOT / ".work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="semantic-", dir=work) as temporary:
        payload = Path(temporary) / "boundaries.i64le"
        values = [-(1 << 63), -1, 0, 1, (1 << 63) - 1, 0, -1, 42, -42]
        payload.write_bytes(b"".join(struct.pack("<q", value) for value in values))
        records = []
        checks = [("C01", "transform"), ("K01", "scan"), ("K01", "sort")]
        for language in ("csharp", "java"):
            record, _ = check_worker(language, payload, checks, config["process_timeout_seconds"])
            records.append(record)
        payload.write_bytes(b"truncated")
        for language in ("csharp", "java"):
            record = run_command(worker_command(language, payload), ROOT, config["process_timeout_seconds"], "QUIT\n")
            if record["exit_code"] == 0:
                raise ValueError(f"{language} accepted a truncated int64 input")
            records.append(record)
        return {"status": "pass", "oracle_checks": 6, "malformed_input_rejections": 2,
                "scope": "C01/K01 numeric boundaries, signed ordering, duplicates and malformed binary input",
                "commands": records}


def compiled_files():
    paths = list((ROOT / "dotnet" / "bin" / "Release" / "net10.0").glob("*"))
    paths += list((ROOT / "java" / "build").rglob("*.class"))
    return {path.relative_to(ROOT).as_posix(): file_hash(path) for path in sorted(paths) if path.is_file()}


def evidence_file(campaign, name, value):
    path = ROOT / "evidence" / campaign / name
    atomic_json(path, value)
    return path.relative_to(ROOT).as_posix(), file_hash(path)


def smoke(config, selectors, campaign_id=None, resume=False):
    selected = dict(selectors)
    selected.setdefault("dataset", "smoke")
    selected.setdefault("repetition", 1)
    rows = inventory(ROOT, config, selected)
    for row in rows:
        row['implemented'] = bool(row['implemented'] and row['case_id'] in ('C01','K01') and row['profile']=='controlled')
    supported = [row for row in rows if row["implemented"]]
    gaps = [row for row in rows if not row["implemented"]]
    flight = preflight(config)
    if flight["status"] != "ready_for_smoke":
        return {"status": "blocked", "preflight": flight}, 2
    builds = build_workers(config)
    validation = semantic_validation(config)
    work = ROOT / ".work" / "data" / selected["dataset"]
    manifest = generate(work, selected["dataset"], config["seed"])
    identity = source_identity()
    binaries = compiled_files()
    versions = read_json(ROOT / "config" / "versions.lock.json")
    fingerprint = canonical_hash({"source": identity["content_hash"], "commit": identity["commit"],
                                  "host": flight["host_manifest_hash"], "config": config,
                                  "versions": versions, "data": manifest, "binaries": binaries,
                                  "inventory": rows})
    if campaign_id is None:
        campaign_id = "smoke-" + fingerprint[:16]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", campaign_id):
        raise ValueError("Campaign ID permits only letters, digits, hyphen and underscore")
    directory = ROOT / "results" / campaign_id
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if not resume:
            raise ValueError(f"Campaign already exists; use --resume --campaign {campaign_id}")
        if existing.get("fingerprint") != fingerprint:
            raise ValueError("Resume rejected: source/environment/config/input/binary/inventory hashes changed")
    else:
        if resume:
            raise ValueError("Resume requested for a campaign with no checkpoint manifest")
        checkpoint = {"campaign_id": campaign_id, "stage": "smoke", "created_utc": utc_now(),
                      "fingerprint": fingerprint, "config": config,
                      "selected_inventory": rows, "gaps": gaps}
        checkpoint["manifest_hash"] = canonical_hash(checkpoint)
        atomic_json(manifest_path, checkpoint)
    common_evidence = {}
    for name, value in (("campaign-manifest.json", read_json(manifest_path)),
                        ("preflight.json", flight), ("builds.json", builds),
                        ("semantic-validation.json", validation), ("source.json", identity),
                        ("binaries.json", binaries), ("input-manifest.json", manifest),
                        ("config.json", config), ("versions.json", versions)):
        existing_evidence = ROOT / "evidence" / campaign_id / name
        if resume and existing_evidence.is_file():
            # Retain the original chronology and logs; recompilation can have different
            # diagnostic text even when its deterministic binary hashes match exactly.
            relative, digest = existing_evidence.relative_to(ROOT).as_posix(), file_hash(existing_evidence)
        else:
            relative, digest = evidence_file(campaign_id, name, value)
        common_evidence[relative] = digest
    completed = resumed = 0
    for row in supported:
        path = directory / "records" / (row["case_hash"] + ".json")
        if path.exists():
            result = read_json(path)
            validate_result(result)
            verify_evidence(ROOT, result)
            if result["fingerprint"] != fingerprint or result["case_hash"] != row["case_hash"]:
                raise ValueError("Resume result identity differs")
            resumed += 1
            continue
        started = utc_now()
        raw, messages = check_worker(row["language"], work / manifest["file"],
                                     [(row["case_id"], row["parameter_id"])], config["process_timeout_seconds"])
        relative, digest = evidence_file(campaign_id, row["case_hash"] + ".worker.json", raw)
        evidence = {**common_evidence, relative: digest}
        pair_identity = {key: row[key] for key in ("case_id", "parameter_id", "engine", "access", "profile",
                                                   "dataset", "workers", "repetition", "track")}
        result = {key: row[key] for key in ("case_id", "parameter_id", "language", "engine", "access", "profile",
                                           "dataset", "workers", "repetition", "track", "case_hash")}
        result.update({"contract_version": "smoke-1", "campaign_id": campaign_id,
                       "pair_id": canonical_hash(pair_identity), "status": "validated", "fingerprint": fingerprint,
                       "provenance": {"utc_start": started, "utc_end": utc_now(), "source_commit": identity["commit"],
                                      "source_hash": identity["content_hash"], "dirty_source": identity["dirty"],
                                      "command": raw["command"], "host_manifest_hash": flight["host_manifest_hash"],
                                      "versions_hash": canonical_hash(versions), "config_hash": canonical_hash(config)},
                       "input": {"dataset_hash": manifest["sha256"], "seed": manifest["seed"],
                                 "encoding": manifest["encoding"], "payload_bytes": manifest["bytes"]},
                       "outcome": {"attempts": 1, "successes": 1, "failures": 0, "correctness": "pass",
                                   "checksum": messages[0]["checksum"], "items": messages[0]["items"]},
                       "performance": {"measured": False, "throughput_ops_s": None, "cpu_seconds": None,
                                       "peak_rss_bytes": None, "p99_ms": None,
                                       "unavailable_reason": "Correctness smoke protocol; no benchmark timing collected"},
                       "admission": {"admitted": False, "reason": "Smoke is never a ranking result"}, "evidence": evidence})
        result["result_hash"] = canonical_hash(result)
        validate_result(result)
        atomic_json(path, result)
        completed += 1
    report = summarize(ROOT, campaign_id)
    report["new_results"] = completed
    report["resumed_results"] = resumed
    atomic_json(directory / "report.json", report)
    return report, 0 if not gaps else 2


def dry_run(config, selectors, stage):
    rows = inventory(ROOT, config, selectors)
    duration = config["warmup_seconds"] + config["measurement_seconds"]
    network = [row for row in rows if row["case_id"].startswith("D") and row["engine"] != "sqlite"]
    sqlite = [row for row in rows if row["case_id"].startswith("D") and row["engine"] == "sqlite"]
    return {"status": "plan_only", "stage": stage, "executed": False,
            "track": config["track"], "generated_inventory_rows": len(rows),
            "authored_case_ids": len({row["case_id"] for row in rows}),
            "rows_by_domain": dict(sorted(Counter(row["case_id"][0] for row in rows).items())),
            "implemented_smoke_rows": sum(bool(row["implemented"]) for row in rows),
            "full_campaign_ready": False,
            "estimated_wall_seconds": {"network_dal_nominal": len(network) * duration,
                                       "sqlite_dal_nominal": len(sqlite) * duration,
                                       "all_rows_nominal": len(rows) * duration},
            "estimate_method": "Inventory rows times configured warmup + measurement; arithmetic only, not measured runtime",
            "estimate_limitations": ["Mutation rows use bounded work; actual duration unknown until pilot",
                                     "Startup, build, micro, HTTP and diagnostic rows have distinct execution boundaries",
                                     "Unfrozen pilot parameters may expand the final inventory; setup/reset costs excluded"],
            "blockers": ["Full campaign requires independently verified admission gates for all mandatory cells",
                         "Provider/image/native dependency locks and parity are incomplete",
                         "No frozen pilot, offered-rate grid or resource allocation",
                         "Full campaign resource budget is not authorized"],
            "performance_index": None}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("preflight", "inventory", "validate", "run", "report"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="config/campaign.json")
        if name in ("inventory", "validate", "run"):
            command.add_argument("--all", action="store_true", help="Select complete catalog, preserving unavailable cases")
            command.add_argument("--language", choices=("csharp", "java"))
            command.add_argument("--engine", choices=("sqlserver", "postgresql", "sqlite"))
            command.add_argument("--access", choices=("orm", "raw"))
            command.add_argument("--case", help="Comma-separated authored case IDs")
            command.add_argument("--dataset", choices=("smoke", "S"))
            command.add_argument("--profile")
            command.add_argument("--workers", type=int)
            command.add_argument("--repetition", type=int)
        if name in ("validate", "run"):
            command.add_argument("--suite", action="store_true", help="Use the protocol-2 complete-suite workers")
            command.add_argument("--legacy", action="store_true", help="Use only the original C01/K01 correctness worker")
        if name == "inventory":
            command.add_argument("--format", choices=("json",), default="json")
        if name == "run":
            command.add_argument("--stage", choices=("smoke", "pilot", "full"), required=True)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--resume", action="store_true")
            command.add_argument("--campaign")
            command.add_argument("--pilot", type=Path, help="Frozen pilot-lock.json for full measured execution")
        if name == "report":
            command.add_argument("--campaign", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        from controller.toolchain import activate_toolchain
        activate_toolchain(ROOT)
        config = resolve_config(args.config)
        selectors = selectors_from(args)
        if args.command == "preflight":
            output = preflight(config)
            code = 0 if output["status"] == "ready_for_smoke" else 2
        elif args.command == "inventory":
            rows = inventory(ROOT, config, selectors)
            output = {"track": config["track"], "authored_definitions": len(load_catalog(ROOT)),
                      "expanded_rows": len(rows), "inventory": rows,
                      "completeness": "Declared variants; pilot-dependent rates/resources remain unresolved"}
            code = 0
        elif args.command == "validate":
            if args.suite or (args.all and not args.legacy):
                from controller.suite import run_suite
                output, code = run_suite(ROOT, config, selectors, stage="smoke")
                print(json.dumps(output, indent=2, sort_keys=True))
                return code
            rows = inventory(ROOT, config, {**selectors, "repetition": selectors.get("repetition", 1)})
            for row in rows:
                row['implemented'] = bool(row['implemented'] and row['case_id'] in ('C01','K01') and row['profile']=='controlled')
            flight = preflight(config)
            if flight["status"] != "ready_for_smoke":
                output, code = {"status": "blocked", "preflight": flight}, 2
            else:
                builds = build_workers(config)
                validation = semantic_validation(config)
                gaps = [row for row in rows if not row["implemented"]]
                output = {"status": "partial" if gaps else "pass", "validation": validation,
                          "selected_rows": len(rows), "unavailable_rows": len(gaps), "builds": builds,
                          "scope": "Implemented C01/K01 semantics only; full-suite parity remains incomplete"}
                code = 2 if gaps else 0
        elif args.command == "run":
            if args.dry_run:
                output, code = dry_run(config, selectors, args.stage), 0
            elif args.suite or (args.all and not args.legacy):
                from controller.suite import run_suite
                output, code = run_suite(ROOT, config, selectors, stage=args.stage,
                                         campaign_id=args.campaign, resume=args.resume, pilot_path=args.pilot)
            elif args.stage != "smoke":
                output, code = dry_run(config, selectors, args.stage), 2
                output["status"] = "blocked"
            else:
                output, code = smoke(config, selectors, args.campaign, args.resume)
        else:
            manifest = read_json(ROOT / "results" / args.campaign / "manifest.json") if re.fullmatch(r"[A-Za-z0-9_-]+", args.campaign) else {}
            if manifest.get("suite"):
                from controller.suite import summarize_suite
                output, code = summarize_suite(ROOT, args.campaign), 0
            else:
                output, code = summarize(ROOT, args.campaign), 0
        print(json.dumps(output, indent=2, sort_keys=True))
        return code
    except (Blocked, ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        print(json.dumps({"status": "blocked" if isinstance(error, Blocked) else "error", "reason": str(error)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
