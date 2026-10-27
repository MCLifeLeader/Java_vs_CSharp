"""Owned database JSONL execution and independent full-state correctness checks."""

import json
import os
import struct
import hashlib
from pathlib import Path
from contextlib import ExitStack

from controller.execution import BatchWorker, ProcessSampler, paired_schedule
from reporting.report import canonical_hash


class DatabaseWorker(BatchWorker):
    def __init__(self, command, cwd, timeout=60, environment=None, limits=None):
        super().__init__(command, cwd, timeout, limits=limits, environment=environment, protocol=None,
                         quit_command=json.dumps({"case_id": "QUIT"}))

    def command_json(self, case_id, **fields):
        self.process.stdin.write(json.dumps({"case_id": case_id, **fields}, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        result = self.read()
        if result.get("status") != "ok" or result.get("case_id") != case_id:
            raise ValueError("Database operation failed: " + str(result.get("reason", "invalid response identity")))
        return result["result"]


def database_command(root, data, row):
    root = Path(root)
    if row["language"] == "csharp":
        command = ["dotnet", str(root / "dotnet/Database/bin/Release/net10.0/Database.dll")]
    else:
        command = ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(root / "java/database/target/database-worker-1.0.0-all.jar")]
    return command + [str(root), row["engine"], row["access"], str(data)]


def operation_trace(data, case_id, count=1, phase="measurement"):
    from validation.database_oracle import new_order
    traces = json.loads((Path(data) / f"trace-{phase}.json").read_text(encoding="utf-8"))
    if count > len(traces):
        raise ValueError("Finite canonical database trace exhausted; do not wrap")
    trace = []
    with (Path(data) / "orders.csv").open(encoding="utf-8") as stream:
        order_count = sum(1 for _ in stream) - 1
    with (Path(data)/'customers.csv').open(encoding='utf-8') as stream:
        customer_count=sum(1 for _ in stream)-1
    raw=(Path(data)/'integers.i64le').read_bytes()
    read_keys=struct.unpack('<'+'q'*(len(raw)//8),raw)
    for index, item in enumerate(traces[:count]):
        case = item["case_id"] if case_id == "D08" else case_id
        key=read_keys[index%len(read_keys)]&((1<<64)-1)
        if case == "D01":
            parameter = {"customer_id": item["customer_id"] if case_id=='D08' else key%customer_count+1}
        elif case == "D02":
            cursor=(key>>2)%max(1,order_count-200)
            parameter = {"status":key%4,"cursor_created_epoch_ms":1700000000000+cursor*1000,"cursor_id":cursor}
        elif case == "D03":
            parameter = {"order_id":key%order_count+1}
        elif case == "D04":
            parameter = {"start_epoch_ms": 0, "end_epoch_ms": 2000000000000}
        elif case == "D05":
            parameter = {"customer_id": item["customer_id"], "delta_cents": item["delta_cents"]}
        elif case == "D06":
            parameter = new_order(item["insert_id"])
            parameter["customer_id"] = item["customer_id"]
        elif case == "D07":
            parameter = {"order_id": item["order_id"] if case_id == "D08" else index + 1 + (order_count // 2 if phase == "measurement" else 0)}
        else:
            raise ValueError("Unknown database workload")
        owner = (parameter["customer_id"] - 1) % 32 if case in ("D01", "D05") else index % 32
        trace.append({"case_id": case, "input": parameter, "worker_partition": owner})
    return trace


def validate_batch(batch, count, workers):
    if (batch.get("attempts") != count or batch.get("successes") != count or batch.get("categorized_failures")
            or batch.get("workers") != workers or not isinstance(batch.get("latencies_ns"), list)
            or len(batch["latencies_ns"]) != count or any(type(value) is not int or value <= 0 for value in batch["latencies_ns"])
            or type(batch.get("wall_seconds")) not in (int,float) or batch["wall_seconds"] <= 0):
        raise ValueError("Database batch lost work, failed, changed workers or emitted invalid latency evidence")


def archive_trace(root,trace):
    from controller.benchmarkctl import atomic_json,file_hash
    path=Path(root)/'.work/suite-traces'/(canonical_hash(trace)+'.json')
    if not path.is_file():
        atomic_json(path,trace)
    elif json.loads(path.read_text(encoding='utf-8'))!=trace:
        raise ValueError('Immutable preregistered database operation trace changed')
    return {str(path.relative_to(root)).replace('\\','/'):file_hash(path)}


def warm_database(worker, row, trace, minimum, maximum=300):
    """Count successful operation time; canonical resets remain untimed."""
    batches, seconds, rates = [], 0.0, []
    while seconds < maximum:
        batch = worker.command_json("BATCH",workload=row["case_id"],trace=trace,iterations=1,workers=row["workers"])
        validate_batch(batch,len(trace),row["workers"])
        seconds += batch["wall_seconds"]
        rates.append(batch["successes"]/batch["wall_seconds"])
        batches.append({"attempts":batch["attempts"],"wall_seconds":batch["wall_seconds"],"rate":rates[-1]})
        stable = len(rates)>=3 and (max(rates[-3:])-min(rates[-3:]))/(sum(rates[-3:])/3)<=.05
        if row["case_id"] in ("D05","D06","D07","D08"):
            worker.command_json("RESET")
        if seconds>=minimum and stable:
            return {"status":"stable","operation_seconds":seconds,"batches":batches,"resets_timed":False}
    return {"status":"unstable","operation_seconds":seconds,"batches":batches,"resets_timed":False}


def run_database_rows(root, data, config, rows, stage, campaign_id, fingerprint, directory, evidence, *, resume=False, pilot=None):
    from controller.benchmarkctl import atomic_json, utc_now
    from controller.suite import process_limits
    from validation.database_oracle import Oracle
    root, data = Path(root), Path(data)
    records, failures = [], []
    if stage == "full":
        schedule = paired_schedule(rows,config["seed"])
        atomic_json(evidence / "database-paired-schedule.json",schedule)
        rows = [row for pair in schedule for row in pair["rows"]]
    for row in rows:
        owned = ExitStack()
        checkpoint = directory / "records" / (row["case_hash"] + ".json")
        if checkpoint.is_file() and resume:
            record = json.loads(checkpoint.read_text(encoding="utf-8"))
            unsigned = {key: value for key, value in record.items() if key != "result_hash"}
            if record.get("fingerprint") != fingerprint or record.get("result_hash") != canonical_hash(unsigned):
                raise ValueError("Database checkpoint content/provenance changed")
            records.append(record)
            continue
        try:
            environment = os.environ.copy()
            if row["engine"] != "sqlite":
                from controller.infrastructure import DatabaseEnvironment
                infrastructure = owned.enter_context(DatabaseEnvironment(root, config, row["engine"]))
                database_name = infrastructure.create_database()
                environment = infrastructure.worker_environment(database_name, row["language"])
            command = database_command(root, data, row)
            with DatabaseWorker(command, root, config["process_timeout_seconds"] if stage=="smoke" else max(600,config["process_timeout_seconds"]), environment=environment,
                                limits=process_limits(config, row) if stage == "full" else None) as worker:
                oracle = Oracle(data)
                if worker.ready["state"] != oracle.state():
                    raise ValueError("Initial canonical database state differs from the independent oracle")
                mutation = row["case_id"] in ("D05","D06","D07","D08")
                count = (100 if row["case_id"] == "D08" else 1) if stage == "smoke" else (config["mutation_operations"] if mutation else config.get("database_read_batch_operations",1000))
                if stage == "full" and mutation:
                    from controller.suite import case_key
                    frozen = (pilot or {}).get("database",{}).get(case_key(row))
                    if not frozen or type(frozen.get("operations")) is not int or frozen["operations"] < 1:
                        raise ValueError("Finite mutation trace size lacks an exact frozen paired pilot")
                    count = frozen["operations"]
                trace = operation_trace(data, row["case_id"], count)
                read_cycle=None
                if stage!='smoke' and not mutation:
                    available=len(json.loads((data/'trace-measurement.json').read_text(encoding='utf-8')))
                    read_cycle=operation_trace(data,row['case_id'],available)
                    trace=read_cycle[:count]
                trace_evidence=archive_trace(root,read_cycle or trace)
                first = trace[0]
                if worker.command_json(first["case_id"],input=first["input"]) != oracle.execute(first["case_id"],first["input"]):
                    raise ValueError("Untimed database result differs from the independent oracle")
                if worker.command_json("RESET") != Oracle(data).state():
                    raise ValueError("Untimed correctness check reset failed")
                oracle = Oracle(data)
                if stage == "smoke":
                    expected = [oracle.execute(item["case_id"], item["input"]) for item in trace]
                    actual = [worker.command_json(item["case_id"], input=item["input"]) for item in trace]
                    if actual != expected or worker.command_json("STATE") != oracle.state():
                        raise ValueError("Database output or complete committed state differs")
                    result = {"status": "validated", "performance": None, "attempts": len(trace), "successes": len(trace),
                              "final_state": oracle.state(), "metadata": worker.ready["metadata"]}
                else:
                    audit_trace=read_cycle or trace
                    expected_digest=hashlib.sha256()
                    for offset in range(0,len(audit_trace),1000):
                        chunk=audit_trace[offset:offset+1000]
                        expected=[]
                        for item in chunk:
                            output=oracle._execute(item['case_id'],item['input'])
                            # Freeze returned objects before later same-customer
                            # mutations update the oracle's live dictionaries.
                            output=json.loads(json.dumps(output))
                            expected.append({'case_id':item['case_id'],'result':output})
                            expected_digest.update((json.dumps(output,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode('utf-8'))
                        actual=worker.command_json('AUDIT',workload=row['case_id'],trace=chunk)
                        if actual!={'operations':len(chunk),'results':expected}:
                            raise ValueError('Complete untimed operation result audit differs from independent oracle')
                    audit={'operations':len(audit_trace),'sha256':expected_digest.hexdigest(),'chunk_operations':1000,'all_typed_results_compared':True}
                    if worker.command_json('RESET')!=Oracle(data).state():
                        raise ValueError('Post-audit canonical state reset failed')
                    oracle=Oracle(data)
                    # Warmup mutation population is independent and reset while
                    # retaining the runtime/factory/SQLite anchor outside timing.
                    available = len(json.loads((data / "trace-warmup.json").read_text(encoding="utf-8")))
                    warm_trace = read_cycle or operation_trace(data,row["case_id"],min(count,available),"warmup")
                    trace_evidence.update(archive_trace(root,warm_trace))
                    warmup = warm_database(worker,row,warm_trace,config["warmup_seconds"] if stage == "full" else config.get("database_pilot_warmup_seconds",1))
                    if worker.command_json("RESET") != oracle.state():
                        raise ValueError("Warm factory reset differs from initial canonical database")
                    batches = []
                    read_cursor=0
                    sampler = ProcessSampler(worker.process.pid)
                    database_before = infrastructure.resource_snapshot() if row["engine"]!="sqlite" else None
                    try:
                        sampler.start()
                        target = config["measurement_seconds"] if stage == "full" and not mutation else 0
                        while not batches or sum(batch["wall_seconds"] for batch in batches) < target:
                            if read_cycle:
                                trace=[read_cycle[(read_cursor+index)%len(read_cycle)] for index in range(count)]
                                read_cursor=(read_cursor+count)%len(read_cycle)
                            batch = worker.command_json("BATCH",workload=row["case_id"],trace=trace,iterations=1,workers=row["workers"])
                            validate_batch(batch,len(trace),row["workers"])
                            batches.append(batch)
                        resources = sampler.finish()
                    finally:
                        sampler.close()
                    database_after = infrastructure.resource_snapshot() if row["engine"]!="sqlite" else None
                    if mutation:
                        for item in trace:
                            oracle._execute(item["case_id"],item["input"])
                    if worker.command_json("STATE") != oracle.state():
                        raise ValueError("Post-batch full committed state differs")
                    seconds = sum(batch["wall_seconds"] for batch in batches)
                    successes = sum(batch["successes"] for batch in batches)
                    database_cpu = database_after["cpu_seconds"]-database_before["cpu_seconds"] if database_before else 0
                    diagnostic=None
                    native_parity = False
                    if row['engine']!='sqlite':
                        from controller.admission import network_diagnostics
                        diagnostic=network_diagnostics(root,row,pilot,worker.ready['metadata'],infrastructure.profile['image'])
                        native_parity=diagnostic['passed']
                    if row['engine']=='sqlite':
                        wanted = (pilot or {}).get("sqlite_native_parity")
                        native = worker.ready["metadata"]
                        native_parity = bool(wanted and all(native.get(key)==wanted.get(key) for key in ("sqlite_version","sqlite_source_id","compile_options"))
                                             and sorted(item["sha256"] for item in native.get("native_libraries",[]))==wanted.get("native_sha256"))
                    gates = {"oracle_state":True,"all_operations_successful":True,"warmup_stable":warmup["status"]=="stable",
                             "warmup_duration":warmup["operation_seconds"]>=config["warmup_seconds"],
                             "measurement_duration":seconds >= (10 if mutation else config["measurement_seconds"]),
                             "collector":not resources["errors"],"resource_limits":worker.budget is not None and worker.budget.evidence["enforced"],
                             "native_provider_parity":native_parity,"database_cpu_measured":database_cpu>=0}
                    result = {"status":"pilot" if stage=="pilot" else "measured","batches":batches,"warmup":warmup,"gates":gates,
                              "resource_budget":worker.budget.evidence if worker.budget else None,
                              "semantic_audit":audit,
                              "network_diagnostics":diagnostic,
                              "database_pilot":{"operations":count,"duration_seconds":seconds,"mutation":mutation},
                              "metadata": worker.ready["metadata"], "final_state": oracle.state(),
                              "database_resources":{"before":database_before,"after":database_after,"cpu_seconds":database_cpu,
                                                    "rss_bytes":None,"rss_reason":"Container cgroup memory is separately labeled; no misleading RSS conversion"},
                              "performance":{"throughput_per_second":successes/seconds,"cpu_seconds":resources["cpu_seconds"],
                                             "cpu_per_work_unit":resources["cpu_seconds"]/successes,"peak_rss_bytes":resources["peak_rss_bytes"],
                                             "total_cpu_per_work_unit":(resources["cpu_seconds"]+database_cpu)/successes},
                              "resources": resources}
                atomic_json(evidence / (row["case_hash"] + ".database.json"),
                            {"command": command, "ready": worker.ready, "stdout": worker.stdout, "stderr": worker.stderr})
            if row["engine"] != "sqlite":
                infrastructure.drop_database(database_name)
                atomic_json(evidence / (row["case_hash"] + ".infrastructure.json"),
                            {"engine": row["engine"], "owner": infrastructure.owner, "image": infrastructure.profile["image"],
                             "commands": infrastructure.logs, "network_profile": "isolated-loopback-lab TLS-off"})
            result.update({"contract_version": "suite-1", "campaign_id": campaign_id, "identity": row,
                           "trace_evidence":trace_evidence,
                           "fingerprint": fingerprint, "command": command, "utc_completed": utc_now(), "admitted": False,
                           "reason":"Admission requires all recorded warmup, trace, resource, engine parity and complete CPU gates"})
            if stage == "full":
                result["admitted"] = all(result.get("gates",{}).values())
            result["result_hash"] = canonical_hash(result)
            atomic_json(checkpoint, result)
            records.append(result)
        except Exception as error:
            failure = {"identity": row, "status": "blocked_or_invalid", "reason": str(error), "admitted": False}
            atomic_json(directory / "failures" / (row["case_hash"] + ".json"), failure)
            failures.append(failure)
        finally:
            owned.close()
    return records, failures
