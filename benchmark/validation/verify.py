"""Run validation and preserve test definitions, discovery, subtests and execution separately."""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_inventory():
    repository = ROOT.parent
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    paths = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=repository).decode().split("\0")
    files = {name: sha(repository / name) for name in sorted(set(paths)) if name and (repository / name).is_file() and not name.startswith("docs/verification/")}
    return {"base_commit": head, "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repository)),
            "files": files, "content_sha256": hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "method": "git ls-files cached+untracked excluding Git-ignored files; curated verification output excluded to avoid self-reference"}


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *arguments, **keywords):
        super().__init__(*arguments, **keywords)
        self.records, self.subtest_records = [], []
        self.current = None

    def startTest(self, test):
        super().startTest(test)
        self.current = {"id": test.id(), "area": test.id().split(".")[0], "status": "running"}
        self.records.append(self.current)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.current["status"] = "passed"

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.current.update(status="failed", reason=str(error[1]))

    def addError(self, test, error):
        super().addError(test, error)
        self.current.update(status="error", reason=str(error[1]))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.current.update(status="skipped", reason=reason)

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        self.subtest_records.append({"parent": test.id(), "id": str(subtest), "status": "failed" if error else "passed"})
        if error:
            self.current.update(status="failed", reason=str(error[1]))


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


def java_tests():
    maven = os.environ.get("BENCH_MAVEN") or shutil.which("mvn") or str(ROOT / ".work/tools/apache-maven-3.9.11/bin" / ("mvn.cmd" if os.name == "nt" else "mvn"))
    command = [maven, "-B", "-q", "test"]
    from controller.benchmarkctl import run_command
    raw = run_command(command, ROOT / "java/suite", timeout=300)
    completed = subprocess.CompletedProcess(command, raw["exit_code"], raw["stdout"], raw["stderr"])
    log = ROOT / ".work/java-junit.log"
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    cases, reports = [], []
    for path in sorted((ROOT / "java/suite/target/surefire-reports").glob("TEST-*.xml")):
        reports.append({"file": str(path.relative_to(ROOT)), "sha256": sha(path)})
        for case in ET.parse(path).getroot().iter("testcase"):
            status = "failed" if case.find("failure") is not None else "error" if case.find("error") is not None else "skipped" if case.find("skipped") is not None else "passed"
            cases.append({"id": case.attrib["classname"] + "." + case.attrib["name"], "status": status})
    authored = sum(len(__import__("re").findall(r"@Test\b", path.read_text(encoding="utf-8"))) for path in (ROOT / "java/suite/src/test").rglob("*.java"))
    return {"framework": "JUnit5/Surefire", "authored_definitions": authored, "discovered_cases": len(cases), "results": dict(Counter(case["status"] for case in cases)),
            "command": command, "exit_code": completed.returncode, "cases": cases, "reports": reports, "log_sha256": sha(log)}


def main():
    from controller.toolchain import activate_toolchain
    activate_toolchain(ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java", action="store_true", help="Run Java JUnit tests as well as the Python integration suite")
    parser.add_argument("--output", type=Path, default=ROOT / ".work/verification.json")
    args = parser.parse_args()
    from controller.clean_start import RunLease
    with RunLease(ROOT, kind="verification") as lease:
        previous_temporary_root = tempfile.tempdir
        tempfile.tempdir = lease.environment()["BENCH_SCRATCH_ROOT"]
        try:
            return verify(args, lease)
        finally:
            tempfile.tempdir = previous_temporary_root


def verify(args, lease):
    before = source_inventory()
    authored = {}
    for path in sorted((ROOT / "validation").glob("test_*.py")):
        authored[path.stem] = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_") for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "validation"), "test_*.py")
    discovered = list(flatten(suite))
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    areas = {}
    for area, definitions in authored.items():
        records = [record for record in result.records if record["area"] == area]
        areas[area] = {"authored_definitions": definitions, "discovered_cases": sum(identifier.startswith(area + ".") for identifier in discovered),
                       "executed_results": dict(Counter(record["status"] for record in records)),
                       "subtest_results": dict(Counter(row["status"] for row in result.subtest_records if row["parent"].startswith(area + ".")))}
    java = java_tests() if args.java else {"framework": "JUnit5/Surefire", "status": "not_run"}
    after = source_inventory()
    evidence = {"recorded_utc": datetime.now(timezone.utc).isoformat(), "source": before,
                "clean_start": lease.clean_start_report,
                "source_changed_during_verification": before["content_sha256"] != after["content_sha256"],
                "python": {"framework": "unittest", "runtime": sys.version, "authored_definitions": sum(authored.values()),
                           "discovered_cases": len(discovered), "tests_run": result.testsRun,
                           "results": dict(Counter(record["status"] for record in result.records)), "areas": areas,
                           "cases": result.records, "subtests": result.subtest_records}, "java": java,
                "coverage": {"status": "unavailable", "reason": "No measured coverage report"},
                "benchmark_inventory": "55 authored workload IDs and 180 parameter definitions; these are not unit-test totals",
                "method": "AST authored Python test methods; unittest discovery and execution; subtests separate. JUnit @Test definitions and fresh Surefire XML cases. No snapshots/browser repetitions/standalone script counts added."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(args.output), "python": evidence["python"]["results"], "java": java.get("results"), "source_changed": evidence["source_changed_during_verification"]}))
    return 0 if result.wasSuccessful() and java.get("exit_code", 0) == 0 and not evidence["source_changed_during_verification"] else 1


if __name__ == "__main__":
    sys.exit(main())
