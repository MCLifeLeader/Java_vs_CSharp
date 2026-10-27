"""Common external batch timing, worker-only OS metrics and reproducible pair order."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import random
import subprocess
import threading
import time
from collections import deque


def paired_schedule(rows, seed=20261025):
    groups = {}
    for row in rows:
        key = tuple((field, row.get(field)) for field in
                    ("case_id", "parameter_id", "engine", "access", "profile", "dataset", "workers", "repetition", "track"))
        if row["language"] in groups.setdefault(key, {}):
            raise ValueError("Duplicate language in a measurement pair")
        groups[key][row["language"]] = row
    keys = sorted(groups, key=repr)
    rng = random.Random(seed)
    rng.shuffle(keys)
    first = ["csharp", "java"] * (len(keys) // 2) + (["csharp"] if len(keys) % 2 else [])
    rng.shuffle(first)
    schedule = []
    for key, first_language in zip(keys, first, strict=True):
        pair = groups[key]
        if set(pair) != {"csharp", "java"}:
            raise ValueError("Measured campaigns require complete paired language selections")
        pair_id = hashlib.sha256(json.dumps(dict(key), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        order = [first_language, "java" if first_language == "csharp" else "csharp"]
        schedule.append({"pair_id": pair_id, "order": order, "rows": [pair[language] for language in order]})
    return schedule


class ProcessSampler:
    """Sample one worker PID through the same OS interface for both languages."""

    def __init__(self, pid, interval=0.1):
        if interval <= 0:
            raise ValueError("Sampling interval must be positive")
        self.pid, self.interval = pid, interval
        self.samples, self.errors = [], []
        self.stop = threading.Event()
        self.thread = self.handle = None
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            self.kernel.GetProcessTimes.restype = wintypes.BOOL
            self.handle = self.kernel.OpenProcess(0x0400 | 0x0010, False, pid)
            if not self.handle:
                raise OSError(ctypes.get_last_error(), "Cannot sample worker process")

    def snapshot(self):
        now = time.perf_counter()
        if os.name == "nt":
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                    "PeakPagefileUsage", "PrivateUsage")]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            if not self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error(), "Cannot read worker RSS")
            creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(self.handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
                raise OSError(ctypes.get_last_error(), "Cannot read worker CPU")
            ticks = ((kernel.dwHighDateTime << 32) + kernel.dwLowDateTime + (user.dwHighDateTime << 32) + user.dwLowDateTime)
            return {"monotonic_seconds": now, "cpu_seconds": ticks / 10_000_000, "rss_bytes": int(counters.WorkingSetSize)}
        if Path("/proc").is_dir():
            fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            resident = int(Path(f"/proc/{self.pid}/statm").read_text().split()[1])
            return {"monotonic_seconds": now, "cpu_seconds": ticks / os.sysconf("SC_CLK_TCK"), "rss_bytes": resident * os.sysconf("SC_PAGE_SIZE")}
        raise OSError("Common worker-only OS collector supports Windows and Linux")

    def start(self):
        self.samples = [self.snapshot()]
        self.stop.clear()
        def collect():
            while not self.stop.wait(self.interval):
                try:
                    self.samples.append(self.snapshot())
                except OSError as error:
                    self.errors.append(str(error))
                    return
        self.thread = threading.Thread(target=collect, daemon=True)
        self.thread.start()
        return self

    def finish(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=max(1, self.interval * 2))
        self.samples.append(self.snapshot())
        return {"cpu_seconds": self.samples[-1]["cpu_seconds"] - self.samples[0]["cpu_seconds"],
                "peak_rss_bytes": max(row["rss_bytes"] for row in self.samples), "sample_interval_seconds": self.interval,
                "samples": self.samples, "collector": "GetProcessTimes/GetProcessMemoryInfo" if os.name == "nt" else "Linux procfs stat/statm", "errors": self.errors}

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class BatchWorker:
    """Long-lived protocol-2 worker. Preparation and CHECK are outside timing."""

    def __init__(self, command, cwd, timeout=60, limits=None, environment=None, protocol=2, quit_command="QUIT"):
        from controller.clean_start import current_run
        self.lease = current_run()
        if self.lease is not None:
            environment = {**(environment if environment is not None else os.environ), **self.lease.environment()}
        self.command, self.cwd, self.timeout = [str(part) for part in command], str(cwd), timeout
        self.stdout, self.stderr = [], []
        self.quit_command = quit_command
        self.responses = queue.Queue()
        self.process = subprocess.Popen(self.command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="strict", bufsize=1, env=environment,
                                        start_new_session=os.name != "nt")
        self.budget = None
        if self.lease is not None:
            try:
                self.lease.track_process(self.process)
            except BaseException:
                self.process.kill()
                self.process.wait(timeout=5)
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
                raise
        if limits is not None:
            from controller.resources import ProcessBudget
            try:
                self.budget = ProcessBudget(self.process.pid, limits)
            except BaseException:
                self.close()
                raise
        def read_stdout():
            try:
                for line in self.process.stdout:
                    self.stdout.append(line)
                    self.responses.put(line)
            finally:
                self.responses.put(None)
        def read_stderr():
            for line in self.process.stderr:
                self.stderr.append(line)
        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()
        try:
            self.ready = self.read()
            if self.ready.get("status") != "ready" or (protocol is not None and self.ready.get("protocol") != protocol):
                raise ValueError("Protocol-2 worker readiness missing")
        except BaseException:
            self.close()
            raise

    def read(self):
        try:
            line = self.responses.get(timeout=self.timeout)
        except queue.Empty as error:
            raise TimeoutError("Worker did not respond before the command deadline") from error
        if line is None:
            raise ValueError("Worker exited without its required response")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise ValueError("Worker protocol response must be an object")
        return result

    def request(self, command, case_id, parameter_id, iterations=None):
        fields = [command, case_id, parameter_id]
        if iterations is not None:
            if type(iterations) is not int or iterations < 1:
                raise ValueError("Batch iterations must be a positive integer")
            fields.append(str(iterations))
        if any("\t" in field or "\n" in field for field in fields):
            raise ValueError("Invalid command field")
        self.process.stdin.write("\t".join(fields) + "\n")
        self.process.stdin.flush()
        result = self.read()
        required_status = "prepared" if command == "PREPARE" else "ok"
        if result.get("status") != required_status or result.get("case_id") != case_id or result.get("parameter_id") != parameter_id:
            raise ValueError("Worker returned an incorrect case identity or failed operation")
        return result

    def check(self, case_id, parameter_id):
        self.request("PREPARE", case_id, parameter_id, 1)
        return self.request("CHECK", case_id, parameter_id)

    def batch(self, case_id, parameter_id, iterations, expected_checksum=None, collect=True):
        self.request("PREPARE", case_id, parameter_id, iterations)
        sampler = ProcessSampler(self.process.pid) if collect else None
        try:
            if sampler:
                sampler.start()
            started = time.perf_counter_ns()
            output = self.request("BATCH", case_id, parameter_id, iterations)
            elapsed_ns = time.perf_counter_ns() - started
            resources = sampler.finish() if sampler else None
        finally:
            if sampler:
                sampler.close()
        if output.get("iterations") != iterations:
            raise ValueError("Worker completed fewer required batch iterations")
        checksum = output.get("checksum")
        if not isinstance(checksum, str) or len(checksum) != 16:
            raise ValueError("Missing unsigned 64-bit batch output checksum")
        if expected_checksum is not None:
            wanted = (int(expected_checksum, 16) * iterations) & ((1 << 64) - 1)
            if int(checksum, 16) != wanted:
                raise ValueError("Batch checksum differs from the untimed correctness result")
        units = output.get("work_units")
        if type(units) is not int or units <= 0 or not isinstance(output.get("unit"), str) or not output["unit"].isascii() or not output["unit"].replace("_", "").isalpha():
            raise ValueError("Missing positive batch work units and declared unit")
        return {"elapsed_ns": elapsed_ns, "successful_work_units": units, "unit": output["unit"],
                "throughput_per_second": units / (elapsed_ns / 1_000_000_000), "output": output,
                "resources": resources, "boundary": "Controller command send to complete worker reply; worker-only CPU/RSS"}

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write(self.quit_command + "\n")
                    self.process.stdin.flush()
                    self.process.wait(timeout=5)
                except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
        finally:
            try:
                if self.lease is not None:
                    self.lease.process_finished(self.process)
            finally:
                try:
                    if self.budget:
                        self.budget.close()
                finally:
                    for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                        stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def calibrate(worker, case_id, parameter_id, checksum, target_seconds=1.0, max_iterations=1_000_000_000):
    if target_seconds < 1:
        raise ValueError("A score-bearing batch must last at least one second")
    iterations, observations = 1, []
    while True:
        run = worker.batch(case_id, parameter_id, iterations, checksum, collect=False)
        observations.append({"iterations": iterations, "elapsed_ns": run["elapsed_ns"]})
        if run["elapsed_ns"] >= target_seconds * 1_000_000_000:
            return {"iterations": iterations, "observations": observations}
        proposed = max(iterations + 1, math.ceil(iterations * target_seconds * 1_000_000_000 / max(run["elapsed_ns"], 1) * 1.1))
        if proposed > max_iterations:
            raise ValueError("Pilot calibration cannot reach one second within the iteration budget")
        iterations = proposed


def warm_until_stable(worker, case_id, parameter_id, iterations, checksum, minimum_seconds=60,
                      maximum_seconds=300, window_seconds=10, tolerance=0.05):
    if minimum_seconds < 60 or maximum_seconds < minimum_seconds:
        raise ValueError("Warmup requires 60+ seconds and an explicit maximum")
    windows, recent = [], deque(maxlen=3)
    beginning = window_start = time.perf_counter()
    window_units = 0
    while time.perf_counter() - beginning < maximum_seconds:
        run = worker.batch(case_id, parameter_id, iterations, checksum, collect=False)
        window_units += run["successful_work_units"]
        now = time.perf_counter()
        if now - window_start < window_seconds:
            continue
        rate = window_units / (now - window_start)
        recent.append(rate)
        windows.append({"elapsed_seconds": now - beginning, "throughput_per_second": rate})
        window_start, window_units = now, 0
        stable = len(recent) == 3 and max(recent) / min(recent) - 1 <= tolerance
        if now - beginning >= minimum_seconds and stable:
            return {"status": "stable", "windows": windows, "elapsed_seconds": now - beginning,
                    "rule": f"Last three windows range/lowest <= {tolerance:.0%}; throughput proxy only"}
    return {"status": "unstable", "windows": windows, "elapsed_seconds": time.perf_counter() - beginning,
            "reason": "Warmup never met the preregistered throughput stabilization rule"}
