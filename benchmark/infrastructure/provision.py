"""Review or exercise the controller-owned network DB lifecycle without logging secrets."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.infrastructure import DatabaseEnvironment
from controller.clean_start import RunLease, cleanup_owned


def plan(config, engine):
    lock = json.loads((ROOT / "infrastructure/images.lock.json").read_text(encoding="utf-8"))
    profile = config.get("infrastructure", {}).get(engine, {})
    failures = []
    image = profile.get("image")
    if not isinstance(image, str) or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image):
        failures.append("An immutable image digest is required")
    if config.get("example_only"):
        failures.append("Resource examples must be reviewed and copied into an actual campaign")
    cpus = profile.get("cpu_affinity")
    app_cpus = config.get("process_limits", {}).get("cpu_affinity")
    memory = profile.get("memory_bytes")
    if not isinstance(cpus, list) or not cpus or any(type(cpu) is not int or cpu < 0 for cpu in cpus):
        failures.append("Explicit database CPU affinity is required")
    elif not isinstance(app_cpus, list) or not app_cpus or set(cpus).intersection(app_cpus):
        failures.append("Application and database CPU affinity must be explicit and disjoint")
    if type(memory) is not int or memory < 268435456:
        failures.append("Explicit database memory budget is required")
    if engine == "sqlserver" and config.get("sqlserver_eula_accepted") is not True:
        failures.append("SQL Server requires the operator's recorded edition/EULA acceptance")
    return {"engine": engine, "status": "ready" if not failures else "blocked",
            "image": image, "matches_reviewed_image": image == lock[engine]["image"],
            "platform": lock["platform"], "cpu_affinity": cpus, "memory_bytes": memory,
            "reason": failures, "executed": False}


def redact(text, environment, password):
    for secret in [environment.get("BENCH_DB_CONNECTION"), environment.get("BENCH_DB_PASSWORD"), password]:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def execute(config, engine, language, access, data, requests):
    review = plan(config, engine)
    if review["status"] != "ready":
        return review, 2
    command = (["dotnet", str(ROOT / "dotnet/Database/bin/Release/net10.0/Database.dll")] if language == "csharp" else
               ["java", "--enable-native-access=ALL-UNNAMED", "-jar", str(ROOT / "java/database/target/database-worker-1.0.0-all.jar")])
    command += [str(ROOT), engine, access, str(data.resolve())]
    (ROOT / ".work").mkdir(exist_ok=True)
    with RunLease(ROOT, kind="infrastructure-diagnostic") as run, DatabaseEnvironment(ROOT, config, engine) as environment:
        database = environment.create_database()
        worker_environment = environment.worker_environment(database, language)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=worker_environment, shell=False, start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            run.track_process(process)
            stdout, stderr = process.communicate(requests.read_text(encoding="utf-8"), timeout=config.get("process_timeout_seconds", 60))
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise
        finally:
            run.process_finished(process)
        completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        report = {**review, "executed": True, "command": command,
                  "database": database, "owner_token": environment.owner,
                  "exit_code": completed.returncode,
                  "stdout": redact(completed.stdout, worker_environment, environment.password),
                  "stderr": redact(completed.stderr, worker_environment, environment.password),
                  "admitted": False}
        return report, completed.returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "execute", "clean-start"))
    parser.add_argument("--dry-run", action="store_true", help="Read-only clean-start inspection")
    parser.add_argument("--config", type=Path, default=ROOT / "config/campaign.json")
    parser.add_argument("--engine", choices=("postgresql", "sqlserver"))
    parser.add_argument("--language", choices=("csharp", "java"), default="csharp")
    parser.add_argument("--access", choices=("raw", "orm"), default="raw")
    parser.add_argument("--data", type=Path, default=ROOT / ".work/suite-data/smoke")
    parser.add_argument("--requests", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "clean-start":
            report = cleanup_owned(ROOT, dry_run=args.dry_run)
            print(json.dumps(report, indent=2))
            return 0 if report["ready"] else 2
        if args.engine is None:
            raise ValueError("plan/execute require an explicit engine")
        if args.dry_run:
            raise ValueError("--dry-run applies to clean-start; plan is already read-only")
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if args.action == "plan":
            report = plan(config, args.engine)
            code = 0 if report["status"] == "ready" else 2
        else:
            if args.requests is None:
                raise ValueError("execute requires an explicit JSONL request file")
            report, code = execute(config, args.engine, args.language, args.access, args.data, args.requests)
        print(json.dumps(report, indent=2))
        return code
    except Exception as error:
        print(json.dumps({"status": "blocked", "reason": type(error).__name__, "executed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
