"""Protocol-2 suite orchestration with explicit correctness and campaign gates."""

from collections import defaultdict
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from controller.execution import BatchWorker, calibrate, paired_schedule, warm_until_stable
from controller.services import LocalServices
from data.generate_suite import generate_suite
from reporting.report import canonical_hash
from validation.suite_oracle import expected_suite, write_oracles


BATCH_DOMAINS = set("CKMASINL")


def build_suite(root, config):
    from controller.benchmarkctl import run_command, require_success
    root = Path(root)
    commands = []
    for command in (["dotnet", "restore", "Suite.csproj", "--locked-mode"],
                    ["dotnet", "build", "Suite.csproj", "--configuration", "Release", "--no-restore"]):
        commands.append((command, root / "dotnet" / "Suite"))
    maven = os.environ.get("BENCH_MAVEN") or shutil.which("mvn") or shutil.which("mvn.cmd")
    cached = root / ".work" / "tools" / "apache-maven-3.9.11" / "bin" / ("mvn.cmd" if os.name == "nt" else "mvn")
    if not maven and cached.is_file():
        maven = str(cached)
    if not maven:
        raise ValueError("Pinned Maven3.9.11 is unavailable; provision it or set BENCH_MAVEN")
    commands.append(([maven, "-B", "-q", "-DskipTests", "install"], root / "java" / "database"))
    commands.append(([maven, "-B", "-q", "-DskipTests", "package"], root / "java" / "suite"))
    logs = []
    for command, cwd in commands:
        log = run_command(command, cwd, max(60, config["process_timeout_seconds"]))
        logs.append(log)
        require_success(log)
    return logs


def suite_command(root, data, row, server=False):
    root = Path(root)
    if row["language"] == "csharp":
        local_host = root / ".work" / "tools" / "dotnet-runtime" / ("dotnet.exe" if os.name == "nt" else "dotnet")
        command = [str(local_host) if local_host.is_file() else "dotnet", str(root / "dotnet" / "Suite" / "bin" / "Release" / "net10.0" / "Suite.dll")]
    else:
        command = ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(root / "java" / "suite" / "target" / "suite.jar")]
    command += ["--data", str(data), "--scale", row["dataset"], "--workers", str(row["workers"] or 1), "--profile", row["profile"]]
    if server:
        command += ["--server"]
        if row["engine"]:
            command += ["--root", str(root), "--engine", row["engine"], "--access", row["access"]]
        if row["language"] == "csharp":
            command += ["--url", "http://127.0.0.1:0"]
    return command


def lookup_oracle(data, row):
    return expected_suite(data, row["case_id"], row["parameter_id"], scale=row["dataset"],
                          workers=row["workers"] or 1, profile=row["profile"])


def validate_check(check, oracle):
    if check.get("correctness") is not True:
        raise ValueError("Worker correctness gate failed")
    for field in ("checksum", "semantic_hash", "work_units", "unit"):
        if field not in oracle or check.get(field) != oracle[field]:
            raise ValueError(f"Independent oracle mismatch in {field}")
    if "details" in oracle and "values" in oracle["details"]:
        wanted = oracle["details"]["values"]
        observed = (check.get("details") or {}).get("values")
        if not isinstance(observed, list) or len(observed) != len(wanted):
            raise ValueError("Matrix correctness output shape differs")
        for left, right in zip(observed, wanted, strict=True):
            if abs(left - right) > 1e-9 + 1e-9 * abs(right):
                raise ValueError("Matrix output exceeds absolute/relative tolerance")


def validate_worker_ready(worker,row):
    ready=worker.ready
    runtime='.NET 10.0.14' if row['language']=='csharp' else '25.0.3+9-LTS'
    if ready.get('language')!=row['language'] or ready.get('runtime')!=runtime:
        raise ValueError('Suite READY runtime/language differs from the exact LTS manifest')
    if ready.get('scale')!=row['dataset'] or ready.get('workers')!=(row['workers'] or 1):
        raise ValueError('Suite READY dataset/worker budget differs')
    if ready.get('profile',row['profile'])!=row['profile']:
        raise ValueError('Suite READY execution profile differs')


def case_key(row):
    return canonical_hash({key: row[key] for key in
                           ("case_id", "parameter_id", "engine", "access", "profile", "dataset", "workers", "track")})


def process_limits(config, row):
    limits = config.get("process_limits")
    if limits is None:
        return None
    limits = dict(limits)
    if row["case_id"] == "M05":
        limits["memory_bytes"] = (512 if "512" in row["parameter_id"] else 2048) * 1024 * 1024
    return limits


def worker_environment(root,config=None):
    environment = os.environ.copy()
    if config is not None:
        prepare=config.get('prepare_memory_bytes',256*1024*1024)
        memory=(config.get('process_limits') or {}).get('memory_bytes')
        if memory is not None and prepare>memory:
            raise ValueError('Preparation-copy budget exceeds the enforced process memory limit')
        environment['BENCH_PREPARE_BYTES']=str(prepare)
    runtime=Path(root)/'.work/tools/dotnet-runtime'
    if (runtime/('dotnet.exe' if os.name=='nt' else 'dotnet')).is_file():
        environment.setdefault('DOTNET_ROOT',str(runtime.resolve()))
        environment.setdefault('DOTNET_ROOT_X64',str(runtime.resolve()))
    for name in ("bench.dll", "libbench.so", "libbench.dylib"):
        native = Path(root) / ".work" / name
        if native.is_file():
            environment.setdefault("BENCH_NATIVE_LIBRARY", str(native.resolve()))
            break
    return environment


def run_frozen_batches(root, data, config, rows, pilot, fingerprint, campaign_id, directory, evidence, resume):
    from controller.benchmarkctl import atomic_json, utc_now
    records, errors = [], []
    schedule = paired_schedule(rows, config["seed"])
    atomic_json(evidence / "paired-schedule.json", schedule)
    for pair in schedule:
        # A failed/unusable observation stays in evidence. Only an explicit
        # preregistered invalidation permits rerunning the entire matched pair.
        for row in pair["rows"]:
            checkpoint = directory / "records" / (row["case_hash"] + ".json")
            if checkpoint.is_file() and resume:
                existing = json.loads(checkpoint.read_text(encoding="utf-8"))
                unsigned = {key: value for key, value in existing.items() if key != "result_hash"}
                if existing.get("fingerprint") != fingerprint or existing.get("result_hash") != canonical_hash(unsigned):
                    raise ValueError("Measured checkpoint fingerprint/content changed")
                records.append(existing)
                continue
            command = suite_command(root, data, row)
            try:
                frozen = pilot["cases"].get(case_key(row))
                if not frozen or type(frozen.get("iterations")) is not int or frozen["iterations"] < 1:
                    raise ValueError("The exact paired case lacks frozen pilot work")
                if frozen.get('workers')!=(row['workers'] or 1):
                    raise ValueError('Actual worker count differs from the explicitly frozen paired pilot')
                native_harness=None
                if row['case_id'][0] in 'CKS':
                    from controller.harness import run_harness
                    native_harness=run_harness(root,data,row,fingerprint,evidence,limits=process_limits(config,row),
                                               timeout=config.get('harness_timeout_seconds',1800))
                with BatchWorker(command, root, config["process_timeout_seconds"], limits=process_limits(config, row),
                                 environment=worker_environment(root,config)) as worker:
                    validate_worker_ready(worker,row)
                    oracle = lookup_oracle(data, row)
                    check = worker.check(row["case_id"], row["parameter_id"])
                    validate_check(check, oracle)
                    warmup = warm_until_stable(worker, row["case_id"], row["parameter_id"], frozen["iterations"],
                                               check["checksum"], config["warmup_seconds"])
                    batches = []
                    target = 1 if row["case_id"][0] in "CKS" else config["measurement_seconds"]
                    while sum(batch["elapsed_ns"] for batch in batches) < target * 1_000_000_000:
                        batch = worker.batch(row["case_id"], row["parameter_id"], frozen["iterations"], check["checksum"])
                        if batch["successful_work_units"] != check["work_units"] * frozen["iterations"]:
                            raise ValueError("Measured work count differs from the frozen correctness contract")
                        batches.append(batch)
                    final_check = worker.check(row["case_id"], row["parameter_id"])
                    validate_check(final_check, oracle)
                    elapsed = sum(batch["elapsed_ns"] for batch in batches) / 1_000_000_000
                    units = sum(batch["successful_work_units"] for batch in batches)
                    cpu = sum(batch["resources"]["cpu_seconds"] for batch in batches)
                    peak = max(batch["resources"]["peak_rss_bytes"] for batch in batches)
                    collector_errors = [error for batch in batches for error in batch["resources"]["errors"]]
                    native_agreement = None
                    if row["case_id"][0] in "CKS":
                        from controller.harness import corroborates
                        native_agreement = corroborates(elapsed / (frozen["iterations"] * len(batches)), native_harness,
                                                        config.get("harness_agreement_tolerance", .25))
                    gates = {"oracle": True, "equal_frozen_work": True, "batch_duration": all(batch["elapsed_ns"] >= 1_000_000_000 for batch in batches),
                             "warmup_stable": warmup["status"] == "stable", "collector": not collector_errors,
                             "enforced_resource_limits": worker.budget is not None and worker.budget.evidence["enforced"],
                             "native_harness_corroboration": native_agreement["agrees"] if native_agreement else True}
                    gates['frozen_execution_workers']=True
                    result = {"contract_version": "suite-1", "campaign_id": campaign_id, "stage": "full",
                              "identity": row, "fingerprint": fingerprint, "pair_id": pair["pair_id"], "command": command,
                              "resource_budget": worker.budget.evidence if worker.budget else None,
                              "utc_completed": utc_now(), "status": "measured", "check": check, "warmup": warmup,
                              "batches": batches, "performance": {"throughput_per_second": units / elapsed,
                                  "cpu_seconds": cpu, "cpu_per_work_unit": cpu / units, "peak_rss_bytes": peak,
                                  "elapsed_seconds": elapsed, "unit": check["unit"], "successful_work_units": units},
                              "native_harness": native_harness, "native_harness_agreement": native_agreement,
                              "admitted": all(gates.values()), "gates": gates,
                              "reason": "Measurements remain excluded until every disclosed admission gate passes"}
                    result["result_hash"] = canonical_hash(result)
                    atomic_json(checkpoint, result)
                    atomic_json(evidence / (row["case_hash"] + ".worker.json"),
                                {"command": command, "ready": worker.ready, "stdout": worker.stdout, "stderr": worker.stderr})
                    records.append(result)
            except Exception as error:
                failure = {"identity": row, "status": "failed_or_invalid", "reason": str(error), "admitted": False}
                atomic_json(directory / "failures" / (row["case_hash"] + ".json"), failure)
                errors.append(failure)
    return records, errors


def run_suite(root, config, selectors, *, stage="smoke", campaign_id=None, resume=False, pilot_path=None):
    from controller.benchmarkctl import atomic_json, file_hash, inventory, preflight, source_identity, utc_now
    root = Path(root).resolve()
    filters = dict(selectors)
    filters.setdefault("dataset", "smoke" if stage == "smoke" else "S")
    if stage in ("smoke", "pilot"):
        filters.setdefault("repetition", 1)
    rows = inventory(root, config, filters)
    flight = preflight(config)
    if flight["status"] != "ready_for_smoke":
        return {"status": "blocked", "preflight": flight}, 2
    if stage == "full" and not config.get("full_campaign_authorized"):
        return {"status": "blocked", "reason": "Full scored campaign resource budget is not authorized", "executed": False}, 2
    if stage == "full" and not pilot_path:
        return {"status": "blocked", "reason": "Full execution requires --pilot pointing to an immutable pilot lock", "executed": False}, 2
    builds = build_suite(root, config)
    work = root / ".work"
    work.mkdir(exist_ok=True)
    data = work / "suite-data" / filters["dataset"]
    data.mkdir(parents=True, exist_ok=True)
    inputs = generate_suite(data, filters["dataset"], config["seed"],http_operations=config.get('http_trace_operations'))
    inputs["files"] = {key: value for key, value in inputs["files"].items() if key not in ("services.json", "oracle.json")}
    write_oracles(data, filters["dataset"])
    # The shared oracle is generated independently by the fixture generator. No
    # worker output is ever promoted into expected data.
    if not (data / "oracle.json").is_file():
        raise ValueError("Suite fixtures lack an independent oracle.json")
    # Preserve immutable input bytes for old reports when a later campaign uses
    # another seed or regenerates the working fixture directory.
    oracle_hash=file_hash(data/'oracle.json')
    snapshot=root/'.work/suite-inputs'/canonical_hash({'inputs':inputs,'oracle':oracle_hash})
    snapshot.mkdir(parents=True,exist_ok=True)
    for name,metadata in inputs['files'].items():
        destination=snapshot/name
        if not destination.is_file():
            shutil.copy2(data/name,destination)
        if file_hash(destination)!=metadata['sha256'] or destination.stat().st_size!=metadata['bytes']:
            raise ValueError('Archived immutable fixture content differs: '+name)
    if not (snapshot/'oracle.json').is_file():
        shutil.copy2(data/'oracle.json',snapshot/'oracle.json')
    if file_hash(snapshot/'oracle.json')!=oracle_hash:
        raise ValueError('Archived independent oracle content differs')
    data=snapshot
    source = source_identity()
    binaries = {str(path.relative_to(root)): file_hash(path) for path in
                list((root / "dotnet" / "Suite" / "bin" / "Release" / "net10.0").glob("*"))
                + [root / "java" / "suite" / "target" / "suite.jar"] if path.is_file()}
    for relative in (".work/bench.dll", ".work/libbench.so", ".work/libbench.dylib", ".work/tools/dotnet-runtime/dotnet.exe",
                     ".work/tools/dotnet-runtime/dotnet", "dotnet/Database/bin/Release/net10.0/Database.dll",
                     "java/database/target/database-worker-1.0.0-all.jar"):
        binary = root / relative
        if binary.is_file():
            binaries[relative] = file_hash(binary)
    fingerprint = canonical_hash({"source": source, "host": flight["host_manifest_hash"], "config": config,
                                  "inputs": inputs, "oracle": file_hash(data / "oracle.json"), "binaries": binaries,
                                  "inventory": [{key: row[key] for key in row if key != "repetition"} for row in rows]})
    if campaign_id is None:
        campaign_id = stage + "-suite-" + fingerprint[:16]
    if not campaign_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in campaign_id):
        raise ValueError("Invalid campaign ID")
    directory = root / "results" / campaign_id
    evidence = root / "evidence" / campaign_id
    evidence.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        unsigned={key:value for key,value in manifest.items() if key!='manifest_hash'}
        if manifest.get('manifest_hash')!=canonical_hash(unsigned) or json.loads((evidence/'campaign-manifest.json').read_text(encoding='utf-8'))!=manifest:
            raise ValueError('Resume rejected: original campaign manifest content changed')
        if not resume or manifest["fingerprint"] != fingerprint:
            raise ValueError("Existing campaign requires --resume and exactly matching fingerprints")
    else:
        if resume:
            raise ValueError("No checkpoint exists for the requested campaign")
        manifest = {"campaign_id": campaign_id, "stage": stage, "created_utc": utc_now(), "fingerprint": fingerprint,
                    "suite": True,
                    "config": config, "selected_inventory": rows, "source": source, "inputs": inputs,
                    "input_directory":str(data.relative_to(root)).replace('\\','/'),"oracle_sha256":oracle_hash,
                    "binaries": binaries, "host": flight, "builds": builds,
                    "weights": json.loads((root / "config/weights.json").read_text(encoding="utf-8"))}
        manifest["manifest_hash"] = canonical_hash(manifest)
        atomic_json(manifest_path, manifest)
        atomic_json(evidence / "campaign-manifest.json", manifest)
    for name,value in (("preflight.json",flight),("source.json",source),("input-manifest.json",inputs),
                       ("binaries.json",binaries),("config.json",config),("builds.json",builds),
                       ("versions.json",json.loads((root/'config/versions.lock.json').read_text(encoding='utf-8')))):
        target=evidence/name
        if not (resume and target.is_file()):
            atomic_json(target,value)
    if stage == "full":
        pilot = json.loads(Path(pilot_path).read_text(encoding="utf-8"))
        unsigned_pilot = {key: value for key, value in pilot.items() if key != "pilot_hash"}
        if pilot.get("pilot_hash") != canonical_hash(unsigned_pilot):
            raise ValueError("Pilot lock content hash differs")
        if not pilot.get("frozen") or pilot.get("source_hash") != source["content_hash"] or pilot.get("host_hash") != flight["host_manifest_hash"]:
            raise ValueError("Pilot lock is not frozen or its code/environment differs")
        if pilot.get("input_hash") != canonical_hash(inputs):
            raise ValueError("Pilot fixture content differs from the full campaign")
    else:
        pilot = None
    records, errors = [], []
    with LocalServices(root, data) as services:
        atomic_json(evidence / "services.json", services.manifest)
        from controller.database import run_database_rows
        database_rows = [row for row in rows if row["case_id"][0] == "D"]
        database_records, database_errors = run_database_rows(root, data, config, database_rows, stage,
                                                              campaign_id, fingerprint, directory, evidence,
                                                              resume=resume, pilot=pilot)
        records.extend(database_records)
        errors.extend(database_errors)
        from controller.suite_http import run_http_rows
        http_rows = [row for row in rows if row["case_id"][0] in "HP" or row["case_id"] == "L05"]
        http_records, http_errors = run_http_rows(root, data, config, http_rows, stage, campaign_id, fingerprint,
                                                 directory, evidence, pilot=pilot, resume=resume)
        records.extend(http_records)
        errors.extend(http_errors)
        # Fresh process per macro/full repetition; smoke can reuse preparation
        # across cases because its results have no measured independence claim.
        groups = defaultdict(list)
        if stage == "full":
            generic_rows = [row for row in rows if row["case_id"][0] in BATCH_DOMAINS and row["case_id"] != "L05"]
            full_records, full_errors = run_frozen_batches(root, data, config, generic_rows, pilot, fingerprint,
                                                         campaign_id, directory, evidence, resume)
            records.extend(full_records)
            errors.extend(full_errors)
        for row in rows:
            if stage == "full":
                continue
            key = (row["language"], row["workers"] or 1, row["profile"], row["repetition"] if stage == "full" else 1)
            groups[key].append(row)
        for group in groups.values():
            generic = [row for row in group if row["case_id"][0] in BATCH_DOMAINS and row["case_id"] != "L05"]
            if not generic:
                continue
            first = generic[0]
            command = suite_command(root, data, first)
            try:
                with BatchWorker(command, root, config["process_timeout_seconds"], environment=worker_environment(root,config)) as worker:
                    validate_worker_ready(worker,first)
                    for row in generic:
                        checkpoint = directory / "records" / (row["case_hash"] + ".json")
                        if checkpoint.exists() and resume:
                            existing = json.loads(checkpoint.read_text(encoding="utf-8"))
                            unsigned = {key:value for key,value in existing.items() if key!='result_hash'}
                            if existing.get("fingerprint") != fingerprint or existing.get('result_hash')!=canonical_hash(unsigned):
                                raise ValueError("Result checkpoint fingerprint differs")
                            records.append(existing)
                            continue
                        try:
                            oracle = lookup_oracle(data, row)
                            check = worker.check(row["case_id"], row["parameter_id"])
                            validate_check(check, oracle)
                            if stage == "smoke":
                                batch = worker.batch(row["case_id"], row["parameter_id"], 1, check["checksum"], collect=False)
                                result = {"status": "validated", "check": check, "batch_output": batch["output"],
                                          "performance": None, "admitted": False, "reason": "Functional smoke only; never scored"}
                            elif stage == "pilot":
                                calibration = calibrate(worker, row["case_id"], row["parameter_id"], check["checksum"])
                                result = {"status": "pilot", "calibration": calibration, "check": check,
                                          "performance": None, "admitted": False, "reason": "Pilot calibration is not a ranking result"}
                            result.update({"contract_version": "suite-1", "campaign_id": campaign_id,
                                           "identity": row, "fingerprint": fingerprint, "command": command,
                                           "utc_completed": utc_now()})
                            result["result_hash"] = canonical_hash(result)
                            atomic_json(checkpoint, result)
                            records.append(result)
                        except Exception as error:
                            failure = {"identity": row, "status": "invalid_or_blocked", "reason": str(error), "admitted": False}
                            atomic_json(directory / "failures" / (row["case_hash"] + ".json"), failure)
                            errors.append(failure)
                    atomic_json(evidence / (canonical_hash(command) + ".worker.json"),
                                {"command": command, "ready": worker.ready, "stdout": worker.stdout, "stderr": worker.stderr})
            except Exception as error:
                errors.extend({"identity": row, "status": "blocked", "reason": str(error)} for row in generic)
    handled = {record["identity"]["case_hash"] for record in records} | {error["identity"]["case_hash"] for error in errors}
    from reporting.suite_validation import finalize_record
    from reporting.pair_gates import pair_gate_results
    pair_gate_results(records,stage)
    for record in records:
        finalize_record(root,record,manifest,evidence)
        atomic_json(directory / "records" / (record["identity"]["case_hash"]+".json"),record)
    for row in rows:
        if row["case_hash"] not in handled:
            errors.append({"identity": row, "status": "blocked", "reason": "Requires its database, HTTP, startup or packaging execution boundary"})
    if stage == "pilot":
        paired_calibration = defaultdict(dict)
        paired_workers={}
        http_calibration = defaultdict(dict)
        http_caps={}
        database_calibration = defaultdict(dict)
        for record in records:
            if "database_pilot" in record:
                database_calibration[case_key(record["identity"])][record["identity"]["language"]] = record["database_pilot"]
            if "pilot_loads" in record:
                http_caps[record['http_pilot_key']]=record['pilot_loads'][0]['connection_cap']
                passing = [load["offered_rate"] for load in record["pilot_loads"] if load["slo"]["slo_satisfied"]
                           and load.get('server_drained') and load.get('oracle_state_matches',True) and not load['resources']['errors']]
                if passing:
                    http_calibration[record["http_pilot_key"]][record["identity"]["language"]] = max(passing)
            if "calibration" not in record:
                continue
            paired_calibration[case_key(record["identity"])][record["identity"]["language"]] = record["calibration"]["iterations"]
            paired_workers[case_key(record['identity'])]=record['identity']['workers'] or 1
        lock = {"contract_version": "pilot-1", "frozen": False, "source_hash": source["content_hash"],
                "host_hash": flight["host_manifest_hash"], "input_hash": canonical_hash(inputs),
                "reason": "Review pilot warmup, services, resources, trace sizing and all compatibility evidence before freezing",
                "database": {key:{"operations":max(item["operations"] for item in languages.values()),"per_language":languages}
                             for key,languages in database_calibration.items() if set(languages)=={"csharp","java"}},
                "http": {key: {"csharp_sustainable": languages["csharp"], "java_sustainable": languages["java"],
                               "common_capacity":max(languages.values()),"connection_cap":http_caps[key],"deadline_seconds":15,
                               "common70":.7*min(languages.values()),"grid_factors":[.25,.5,.75,.9,1,1.1]} for key,languages in http_calibration.items()
                         if set(languages) == {"csharp","java"}},
                "cases": {key: {"iterations": max(languages.values()), "workers":paired_workers[key],"per_language_calibration": languages}
                          for key, languages in paired_calibration.items() if set(languages) == {"csharp", "java"}}}
        lock["pilot_hash"] = canonical_hash(lock)
        atomic_json(directory / "pilot-lock.json", lock)
    report = {"campaign_id": campaign_id, "stage": stage, "track": "lts", "status": "complete_selected" if not errors else "partial",
              "selected_rows": len(rows), "completed_rows": len(records), "failed_or_blocked_rows": len(errors),
              "authored_case_ids_completed": sorted({record["identity"]["case_id"] for record in records}),
              "performance_index": None, "performance_reason": "No complete admitted frozen repeated campaign",
              "code_coverage": None, "errors": errors, "records": [record["identity"] for record in records]}
    atomic_json(directory / "report.json", report)
    return report, 0 if not errors else 2


def summarize_suite(root, campaign_id):
    from controller.benchmarkctl import read_json
    from reporting.report import enforce_schema
    root = Path(root).resolve()
    if not campaign_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in campaign_id):
        raise ValueError("Invalid campaign ID")
    directory = root / "results" / campaign_id
    manifest = read_json(directory / "manifest.json")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if manifest.get("manifest_hash") != canonical_hash(unsigned):
        raise ValueError("Suite manifest content hash differs")
    if read_json(root / "evidence" / campaign_id / "campaign-manifest.json") != manifest:
        raise ValueError("Suite manifest differs from its original evidence copy")
    if manifest.get('input_directory'):
        from controller.benchmarkctl import file_hash
        inputs=(root/manifest['input_directory']).resolve()
        if not inputs.is_relative_to(root):
            raise ValueError('Archived input directory escapes benchmark root')
        for name,metadata in manifest['inputs']['files'].items():
            path=(inputs/name).resolve()
            if not path.is_relative_to(inputs) or file_hash(path)!=metadata['sha256'] or path.stat().st_size!=metadata['bytes']:
                raise ValueError('Archived input bytes differ from campaign manifest')
        if file_hash(inputs/'oracle.json')!=manifest['oracle_sha256']:
            raise ValueError('Archived oracle differs from campaign manifest')
    expected_rows = {row["case_hash"]: row for row in manifest["selected_inventory"]}
    if len(expected_rows)!=len(manifest['selected_inventory']):
        raise ValueError('Duplicate selected inventory identities')
    records = []
    schema = read_json(root / "config" / "suite-result.schema.json")
    seen = set()
    for path in sorted((directory / "records").glob("*.json")):
        record = read_json(path)
        enforce_schema(record, schema)
        unsigned_record = {key: value for key, value in record.items() if key != "result_hash"}
        if record.get("result_hash") != canonical_hash(unsigned_record) or record.get("fingerprint") != manifest["fingerprint"]:
            raise ValueError("Suite result content or provenance fingerprint differs")
        identity = record["identity"]
        if identity['case_hash'] in seen or path.stem!=identity['case_hash']:
            raise ValueError('Duplicate or misnamed suite result identity')
        seen.add(identity['case_hash'])
        if expected_rows.get(identity["case_hash"]) != identity or record["campaign_id"] != campaign_id:
            raise ValueError("Suite result identity differs from selected inventory")
        if manifest["stage"] == "smoke" and (record["performance"] is not None or record["admitted"]):
            raise ValueError("Suite smoke cannot contain scored measurements")
        from reporting.suite_validation import validate_suite_record
        validate_suite_record(root,record,manifest)
        records.append(record)
    missing = len(expected_rows) - len(records)
    from reporting.campaign import analyze_campaign
    analysis = analyze_campaign(root, manifest, records)
    return {"campaign_id": campaign_id, "stage": manifest["stage"], "track": "lts",
            "status": "complete_selected" if not missing else "partial", "selected_rows": len(expected_rows),
            "completed_rows": len(records), "missing_rows": missing,
            "authored_case_ids_completed": sorted({record["identity"]["case_id"] for record in records}),
            "performance_index": analysis["primary"]["performance_index"], "analysis": analysis,
            "reason": "Complete admitted weighted evidence is required for ranking",
            "code_coverage": None, "code_coverage_reason": "No measured coverage report"}
