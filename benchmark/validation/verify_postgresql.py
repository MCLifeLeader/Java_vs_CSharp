"""Optional live PostgreSQL contract script; creates only leased disposable resources.

This is a standalone integration script, separate from authored unittest totals.
It never accepts SQL Server terms and never admits performance measurements.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.database import database_command, validate_batch
from controller.infrastructure import DatabaseEnvironment
from controller.resources import ProcessBudget
from controller.clean_start import current_run
from infrastructure.provision import redact
from validation.database_oracle import Oracle, new_order


def catalog(environment, database):
    if database not in environment.databases:
        raise ValueError("Catalog target is not controller owned")
    queries = {
        "durability": "SELECT json_build_object('fsync',current_setting('fsync'),'synchronous_commit',current_setting('synchronous_commit'),'full_page_writes',current_setting('full_page_writes'),'default_transaction_isolation',current_setting('default_transaction_isolation'),'server_encoding',current_setting('server_encoding'))",
        "columns": "SELECT json_agg(json_build_object('table',table_name,'column',column_name,'type',data_type,'nullable',is_nullable,'length',character_maximum_length,'collation',collation_name) ORDER BY table_name,ordinal_position) FROM information_schema.columns WHERE table_schema='public'",
        "indexes": "SELECT json_agg(json_build_object('table',tablename,'index',indexname,'definition',indexdef) ORDER BY tablename,indexname) FROM pg_indexes WHERE schemaname='public'",
        "constraints": "SELECT json_agg(json_build_object('name',conname,'kind',contype,'definition',pg_get_constraintdef(oid)) ORDER BY conname) FROM pg_constraint WHERE connamespace=(SELECT oid FROM pg_namespace WHERE nspname='public')",
    }
    result = {"database": database, "image": environment.profile["image"]}
    for name, query in queries.items():
        result[name] = json.loads(environment.docker(["exec", environment.container, "psql", "-U", "benchmark", "-d", database,
                                                    "-At", "-v", "ON_ERROR_STOP=1", "-c", query]))
    return result


def requests_and_expected(data):
    operations = [
        ("D01", {"customer_id": 1}), ("D02", {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}),
        ("D03", {"order_id": 1}), ("D04", {"start_epoch_ms": 0, "end_epoch_ms": 2000000000000}),
        ("D05", {"customer_id": 1, "delta_cents": 125}),
        ("D05", {"customer_id": 1, "delta_cents": 125, "expected_version": 0}),
        ("D06", new_order(10000)), ("D03", {"order_id": 10000}),
        ("D06", new_order(10000)), ("D06", new_order(10001, inject_failure=True)),
        ("D07", {"order_id": 2, "expected_version": 999}), ("D07", {"order_id": 2}),
        ("H06", new_order(10002)), ("H06", new_order(10003, inject_failure=True)),
    ]
    mixed = ([{"case_id": "D01", "input": {"customer_id": 1}} for _ in range(12)] +
             [{"case_id": "D02", "input": {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}} for _ in range(3)] +
             [{"case_id": "D05", "input": {"customer_id": 2, "delta_cents": 1}},
              {"case_id": "D05", "input": {"customer_id": 2, "delta_cents": 1, "expected_version": 999}}] +
             [{"case_id": "D06", "input": new_order(11000+i)} for i in range(2)] +
             [{"case_id": "D07", "input": {"order_id": 3}}])
    operations.append(("D08", {"trace": mixed}))
    oracle = Oracle(data)
    initial, expected = oracle.state(), []
    for case, parameters in operations:
        try:
            expected.append({"status": "ok", "result": oracle.execute(case, parameters)})
        except ValueError:
            expected.append({"status": "error"})
    requests = [{"case_id": case, "input": parameters} for case, parameters in operations]
    audit = [
        {"case_id": "D01", "input": {"customer_id": 1}},
        {"case_id": "D02", "input": {"status": 0, "cursor_created_epoch_ms": 0, "cursor_id": 0}},
        {"case_id": "D03", "input": {"order_id": 1}},
        {"case_id": "D04", "input": {"start_epoch_ms": 0, "end_epoch_ms": 2000000000000}},
        {"case_id": "D05", "input": {"customer_id": 3, "delta_cents": 7}},
        {"case_id": "D06", "input": new_order(12000)},
        {"case_id": "H06", "input": new_order(12001)},
        {"case_id": "D07", "input": {"order_id": 4}},
    ]
    requests.append({"case_id": "AUDIT", "workload": "D08", "trace": audit})
    expected.append({"status": "ok", "result": {"operations": len(audit), "results": [
        {"case_id": op["case_id"], "result": oracle.execute(op["case_id"], op["input"])} for op in audit]}})
    ids = list(range(601, 0, -1)) + [12000, 12001, 12000]
    requests.append({"case_id": "COMMITTED", "ids": ids})
    expected.append({"status": "ok", "result": sorted(set(ids) & oracle.tables["orders"].keys())})
    requests += [{"case_id": "RESET", "inject_failure": True}, {"case_id": "STATE"},
                 {"case_id": "D01", "input": {"customer_id": 1}}]
    expected += [{"status": "error"}, {"status": "ok", "result": oracle.state()}, {"status": "error"}]
    requests.append({"case_id": "RESET"})
    expected.append({"status": "ok", "result": initial})
    oracle = Oracle(data)
    requests += [{"case_id": "STATE"},
                 {"case_id": "BATCH", "workload": "D01", "trace": [{"customer_id": 1}, {"customer_id": 2}], "iterations": 2, "workers": 8},
                 {"case_id": "RESET"}, {"case_id": "STATE"}, {"case_id": "QUIT"}]
    return requests, expected, initial, oracle.state()


def verify(config, data, output):
    requests, expected, initial, final = requests_and_expected(data)
    output.parent.mkdir(parents=True, exist_ok=True)
    config_path = output.with_suffix(".config.json")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    request_path = output.with_suffix(".requests.jsonl")
    request_path.write_text("".join(json.dumps(item)+"\n" for item in requests), encoding="utf-8")
    report = {"date_utc": datetime.now(timezone.utc).isoformat(), "engine": "postgresql", "admitted": False,
              "scope": "Standalone four-cell functional integration; no independent performance repetitions",
              "configuration": config, "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "request_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(), "cells": []}
    files = [ROOT/"schema/postgresql.sql", ROOT/"dotnet/Database/bin/Release/net10.0/Database.dll",
             ROOT/"java/database/target/database-worker-1.0.0-all.jar", Path(__file__), ROOT/"validation/database_oracle.py"]
    files += list((ROOT/"queries/postgresql").glob("*.sql")) + list(data.glob("*.csv"))
    report["source_and_input_sha256"] = {str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path):
                                         hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    environment = DatabaseEnvironment(ROOT, config, "postgresql")
    try:
        with environment:
            report["container_id"] = environment.container
            report["image"] = environment.profile["image"]
            report["server_version"] = environment._sql("SELECT version();")
            for language in ("csharp", "java"):
                for access in ("raw", "orm"):
                    name = environment.create_database()
                    variables = environment.worker_environment(name, language)
                    lease = current_run()
                    if lease:
                        variables = {**variables, **lease.environment()}
                    row = {"language": language, "engine": "postgresql", "access": access}
                    command = database_command(ROOT, data, row)
                    process = subprocess.Popen(command, cwd=ROOT, env=variables, stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                               start_new_session=os.name != "nt")
                    if lease:
                        lease.track_process(process)
                    budget = None
                    try:
                        budget = ProcessBudget(process.pid, config["process_limits"])
                        stdout, stderr = process.communicate(request_path.read_text(encoding="utf-8"), timeout=120)
                    except BaseException:
                        process.kill()
                        process.communicate(timeout=5)
                        raise
                    finally:
                        if budget:
                            budget.close()
                        if lease:
                            lease.process_finished(process)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream:
                                stream.close()
                    raw = {"command": command, "exit_code": process.returncode,
                           "stdout": redact(stdout, variables, environment.password),
                           "stderr": redact(stderr, variables, environment.password)}
                    cell_path = output.with_name(output.stem+f".{language}.{access}.json")
                    cell_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
                    result = {**row, "resource_budget": budget.evidence, "evidence": str(cell_path.relative_to(ROOT)),
                              "evidence_sha256": hashlib.sha256(cell_path.read_bytes()).hexdigest(), "status": "passed"}
                    try:
                        messages = [json.loads(line) for line in stdout.splitlines()]
                        if process.returncode or not messages or messages[0].get("status") != "ready":
                            raise ValueError("Worker initialization failed")
                        if messages[0]["state"] != initial:
                            raise ValueError("Initial canonical state differs")
                        for index, wanted in enumerate(expected):
                            actual = messages[index+1]
                            if actual["status"] != wanted["status"] or (wanted["status"] == "ok" and actual["result"] != wanted["result"]):
                                raise ValueError(f"Ordered output differs at operation{index}:{requests[index]['case_id']}")
                        if messages[len(expected)+1]["result"] != final:
                            raise ValueError("Committed final state differs")
                        validate_batch(messages[len(expected)+2]["result"], 4, 8)
                        if messages[-2]["result"] != initial or messages[-1]["result"] != initial:
                            raise ValueError("Warm reset did not restore canonical state")
                        result.update(metadata=messages[0]["metadata"], initial_state=initial, final_state=final,
                                      sequential_requests=len(expected), audit_operations=sum(len(r["trace"]) for r in requests if r["case_id"] == "AUDIT"),
                                      commit_reconciliation_verified=True, parallel_read_attempts=4, reset_verified=True)
                    except Exception as error:
                        result.update(status="failed", reason=str(error))
                    report["cells"].append(result)
                    if "realized_catalog" not in report and result["status"] == "passed":
                        report["realized_catalog"] = catalog(environment, name)
                    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
                    environment.drop_database(name)
                    print(json.dumps({"language": language, "access": access, "status": result["status"], "reason": result.get("reason")}), flush=True)
            report["database_resources"] = environment.resource_snapshot()
    finally:
        report["infrastructure_commands"] = environment.logs
        report["cleanup_complete"] = environment.container is None
        report["status"] = "passed" if len(report["cells"]) == 4 and all(cell["status"] == "passed" for cell in report["cells"]) else "failed"
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Explicit reviewed smoke-only process/DB resource allocations")
    parser.add_argument("--data", type=Path, default=ROOT/".work/suite-data/smoke")
    parser.add_argument("--output", type=Path, default=ROOT/".work/postgresql-validation.json")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    return 0 if verify(config,args.data.resolve(),args.output.resolve())["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
