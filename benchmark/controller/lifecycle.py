"""Real HTTP process startup, idle/load, packaging and rebuild boundaries."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from controller.execution import ProcessSampler
from controller.services import open_loop_http
from reporting.analysis import slo_summary


@contextmanager
def packaged_service(root,data,row,timeout=300):
    """Prepare an actual minimal owned deployment before the startup clock."""
    from controller.benchmarkctl import run_command,require_success
    from controller.suite import suite_command
    from reporting.deployment import deployment_inventory
    root=Path(root).resolve()
    work=root/'.work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='service-deployment-',dir=work) as temporary:
        application=Path(temporary)/'app'
        application.mkdir()
        logs=[]
        command=suite_command(root,data,row,server=True)
        if row['language']=='csharp':
            logs.append(require_success(run_command(['dotnet','publish','Suite.csproj','-c','Release','--no-restore','--no-build',
                                                       '--no-self-contained','--output',str(application)],root/'dotnet/Suite',timeout)))
            command[1]=str(application/'Suite.dll')
        else:
            shutil.copy2(root/'java/suite/target/suite.jar',application/'suite.jar')
            command[command.index('-jar')+1]=str(application/'suite.jar')
        inventory=deployment_inventory(root,row['language'],application)
        inventory['preparation_commands']=logs
        if not inventory['artifacts']:
            raise ValueError('Owned service deployment produced no packaged application')
        yield command,inventory


@contextmanager
def service_process(command, cwd, timeout=60, *, environment=None, limits=None):
    output, errors, messages = [], [], queue.Queue()
    started = time.perf_counter_ns()
    process = subprocess.Popen([str(part) for part in command], cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1, env=environment)
    budget = None
    def stdout():
        for line in process.stdout:
            output.append(line)
            try:
                message = json.loads(line)
                if isinstance(message, dict) and message.get("status") == "ready":
                    messages.put(message)
            except json.JSONDecodeError:
                pass
        messages.put(None)
    def stderr():
        for line in process.stderr:
            errors.append(line)
    threading.Thread(target=stdout, daemon=True).start()
    threading.Thread(target=stderr, daemon=True).start()
    try:
        if limits is not None:
            from controller.resources import ProcessBudget
            budget = ProcessBudget(process.pid, limits)
        try:
            ready = messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("HTTP application did not emit readiness before its deadline") from error
        if ready is None or type(ready.get("port")) is not int or not 0 < ready["port"] <= 65535:
            raise ValueError("HTTP application exited or emitted invalid runtime port")
        yield {"process": process, "ready": ready, "started_ns": started, "ready_ns": time.perf_counter_ns(),
               "stdout": output, "stderr": errors, "command": command, "url": f"http://127.0.0.1:{ready['port']}", "budget": budget}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        if budget:
            budget.close()


def first_response(server, path, expected, method="GET", body=None, validator=None, deadline=15):
    connection = http.client.HTTPConnection("127.0.0.1", server["ready"]["port"], timeout=deadline)
    try:
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = response.read()
        status = 201 if (path.startswith("/order") or path.startswith("/checkout")) and method == "POST" else 200
        if response.status != status or not (validator(payload) if validator else payload == expected):
            raise ValueError("First HTTP response failed its semantic correctness contract")
        return {"status": "correct", "launch_to_first_correct_ns": time.perf_counter_ns() - server["started_ns"],
                "launch_to_ready_ns": server["ready_ns"] - server["started_ns"], "response_bytes": len(payload),
                "measurement_boundary": "Fresh process launch, dependencies ready, warm OS file cache, complete first validated response"}
    finally:
        connection.close()


def http_load(server, path, expected, rate, duration, *, method="GET", body=None, validator=None,
              max_connections=256, deadline=15, expected_status=200, requests=None,drain_callback=None,archive_callback=None):
    sampler = ProcessSampler(server["process"].pid)
    try:
        sampler.start()
        result = open_loop_http(server["url"] + path, rate, duration, expected, method=method, body=body,
                                validator=validator, max_connections=max_connections, deadline_seconds=deadline,
                                expected_status=expected_status, requests=requests)
        if archive_callback:
            archive_callback(result)
        result['server_drained']=drain_callback is None
        if drain_callback:
            try:
                drain_callback()
                result['server_drained']=True
            except (ValueError,OSError,TimeoutError) as error:
                result['drain_error']=str(error)
        result["resources"] = sampler.finish()
    finally:
        sampler.close()
    result["slo"] = slo_summary(result["observations"], duration)
    return result


def idle_load(server, expected, rate, *, smoke=False):
    # Full timeline is fixed by P02. Smoke is explicitly scaled and unscored.
    idle_seconds, load_seconds = (0.1, 0.2) if smoke else (30, 60)
    deadline = time.perf_counter() + idle_seconds
    while time.perf_counter() < deadline:
        time.sleep(min(.1, deadline - time.perf_counter()))
    sampler = ProcessSampler(server["process"].pid)
    try:
        idle = sampler.snapshot()
    finally:
        sampler.close()
    load = http_load(server, "/json?size=1024", expected, rate, load_seconds,
                     validator=lambda value: json.loads(value) == json.loads(expected))
    return {"idle_seconds": idle_seconds, "load_seconds": load_seconds, "idle_rss_bytes": idle["rss_bytes"],
            "load": load, "scaled_smoke": smoke}


def owned_build(root, language, variant, *, timeout=300):
    """Build only an owned staging copy; never clean or edit the real checkout."""
    from controller.benchmarkctl import run_command, require_success
    root = Path(root).resolve()
    if language not in ("csharp", "java") or variant not in ("package", "clean", "no-op", "one-method"):
        raise ValueError("Invalid owned build scenario")
    work = root / ".work"
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-stage-", dir=work, ignore_cleanup_errors=True) as temporary:
        stage = Path(temporary) / "benchmark"
        shutil.copytree(root, stage, ignore=shutil.ignore_patterns(".git", ".work", "results", "evidence", "bin", "obj", "target", "build", "__pycache__"))
        if language == "csharp":
            cwd = stage / "dotnet" / "Suite"
            probe = cwd / "IncrementalProbe.cs"
            probe.write_text("namespace BenchmarkSuite; internal static class IncrementalProbe { internal static int Value() => 1; }\n", encoding="utf-8")
            restore = ["dotnet", "restore", "Suite.csproj", "--locked-mode"]
            build = ["dotnet", "build", "Suite.csproj", "-c", "Release", "--no-restore"]
            clean = ["dotnet", "clean", "Suite.csproj", "-c", "Release"]
            package = ["dotnet", "publish", "Suite.csproj", "-c", "Release", "--no-restore", "--no-self-contained"]
            artifact_dir = cwd / "bin" / "Release" / "net10.0" / "publish" if variant == "package" else cwd / "bin" / "Release" / "net10.0"
        else:
            cwd = stage / "java" / "suite"
            maven = os.environ.get("BENCH_MAVEN") or shutil.which("mvn") or str(root / ".work/tools/apache-maven-3.9.11/bin" / ("mvn.cmd" if os.name == "nt" else "mvn"))
            probe = cwd / "src/main/java/benchmark/suite/IncrementalProbe.java"
            probe.write_text("package benchmark.suite; final class IncrementalProbe { static int value() { return 1; } }\n", encoding="utf-8")
            restore = [maven, "-B", "-q", "dependency:go-offline"]
            build = [maven, "-B", "-q", "-o", "package", "-DskipTests"]
            clean = [maven, "-B", "-q", "-o", "clean"]
            package = build
            artifact_dir = cwd / "target"
        build_environment={**os.environ,'MSBUILDDISABLENODEREUSE':'1','DOTNET_CLI_USE_MSBUILD_SERVER':'0'}
        logs = [require_success(run_command(restore, cwd, timeout,environment=build_environment))]
        # Prime dependency-cached outputs equally before incremental scenarios.
        logs.append(require_success(run_command(build, cwd, timeout,environment=build_environment)))
        if variant in ("package", "clean"):
            logs.append(require_success(run_command(clean, cwd, timeout,environment=build_environment)))
        elif variant == "one-method":
            before = probe.read_text(encoding="utf-8")
            probe.write_text(before.replace("=> 1", "=> 2").replace("return 1", "return 2"), encoding="utf-8")
        command = package if variant == "package" else build
        started = time.perf_counter_ns()
        logs.append(require_success(run_command(command, cwd, timeout,environment=build_environment)))
        elapsed = time.perf_counter_ns() - started
        if language == "java" and variant == "package":
            packaged = stage / "deployment"
            packaged.mkdir()
            shutil.copy2(artifact_dir / "suite.jar", packaged / "suite.jar")
            artifact_dir = packaged
        artifacts = {str(path.relative_to(artifact_dir)): path.stat().st_size for path in artifact_dir.rglob("*") if path.is_file()}
        if not artifacts:
            raise ValueError("Build produced no artifacts")
        deployment = None
        if variant == "package":
            from reporting.deployment import deployment_inventory
            deployment = deployment_inventory(stage, language, artifact_dir, runtime_root=root)
        return {"variant": variant, "language": language, "elapsed_ns": elapsed, "artifacts": artifacts,
                "artifact_bytes": sum(artifacts.values()), "commands": logs, "dependency_downloads_timed": False,
                "runtime_inclusive_bytes": deployment["runtime_inclusive_bytes"] if deployment else None,
                "deployment": deployment, "sbom": deployment["sbom"] if deployment else None,
                "one_method_change": variant == "one-method", "scope": "Owned identical build-probe method in staging; diagnostic, zero aggregate weight"}
