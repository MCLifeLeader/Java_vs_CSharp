"""Compile one shared C library consumed by both language workers."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build():
    source = ROOT / "native/bench.c"
    work = ROOT / ".work"
    work.mkdir(exist_ok=True)
    zig = work / "tools/zig-x86_64-windows-0.17.0/zig.exe"
    if os.name == "nt" and zig.is_file():
        command = [str(zig), "cc", "-target", "x86_64-windows-gnu", "-shared", "-nostdlib", "-fno-stack-protector", "-O3", "-Wl,--entry,bench_dll_entry"]
        artifact = work / "bench.dll"
    else:
        compiler = shlex.split(os.environ["BENCH_CC"]) if "BENCH_CC" in os.environ else [shutil.which("cc") or "cc"]
        if os.name == "nt":
            raise ValueError("Run tools/bootstrap.py --component native for the pinned Windows C compiler")
        artifact = work / ("libbench.dylib" if sys.platform == "darwin" else "libbench.so")
        command = compiler + ["-O3", "-fPIC", "-dynamiclib" if sys.platform == "darwin" else "-shared"]
    command += ["-o", str(artifact), str(source)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(result.stderr)
    metadata = {"command": command, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "artifact": str(artifact),
                "compiler_version": subprocess.check_output(command[:1] + ["version" if command[0].endswith("zig.exe") else "--version"], text=True).strip()}
    (work / "native-build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


if __name__ == "__main__":
    print(json.dumps(build(), indent=2))
