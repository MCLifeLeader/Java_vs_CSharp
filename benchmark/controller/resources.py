"""Enforce owned worker resource budgets without changing host-wide settings."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path


def validate_limits(limits):
    if not isinstance(limits, dict):
        raise ValueError("Explicit process memory and CPU limits are required")
    memory, cpus = limits.get("memory_bytes"), limits.get("cpu_affinity")
    if type(memory) is not int or memory < 64 * 1024 * 1024:
        raise ValueError("Process memory limit must be at least64MiB")
    if (not isinstance(cpus, list) or not cpus or any(type(cpu) is not int or cpu < 0 for cpu in cpus)
            or len(set(cpus)) != len(cpus) or any(cpu >= (os.cpu_count() or 1) for cpu in cpus)):
        raise ValueError("CPU affinity must be a unique allowed logical-CPU list")
    return memory, cpus


class ProcessBudget:
    def __init__(self, pid, limits):
        memory, cpus = validate_limits(limits)
        self.job = self.process = None
        self.evidence = {"memory_bytes": memory, "cpu_affinity": cpus, "enforced": False}
        if os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in
                            ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
            self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.kernel.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
            self.kernel.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t)]
            self.job = self.kernel.CreateJobObjectW(None, None)
            self.process = self.kernel.OpenProcess(0x0100 | 0x0001 | 0x0200 | 0x1000, False, pid)
            if not self.job or not self.process:
                self.close()
                raise OSError(ctypes.get_last_error(), "Cannot create owned process budget")
            information = Extended()
            information.BasicLimitInformation.LimitFlags = 0x2000 | 0x0100
            information.ProcessMemoryLimit = memory
            if not self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(information), ctypes.sizeof(information)):
                self.close()
                raise OSError(ctypes.get_last_error(), "Cannot set owned worker memory limit")
            if not self.kernel.AssignProcessToJobObject(self.job, self.process):
                self.close()
                raise OSError(ctypes.get_last_error(), "Cannot assign owned worker to its job")
            mask = sum(1 << cpu for cpu in cpus)
            if not self.kernel.SetProcessAffinityMask(self.process, mask):
                self.close()
                raise OSError(ctypes.get_last_error(), "Cannot set worker CPU affinity")
            actual, system = ctypes.c_size_t(), ctypes.c_size_t()
            if not self.kernel.GetProcessAffinityMask(self.process, ctypes.byref(actual), ctypes.byref(system)) or actual.value != mask:
                self.close()
                raise OSError("Worker CPU affinity did not match its manifest")
            self.evidence.update(enforced=True, mechanism="Windows per-process JobObject commit-memory limit and affinity",
                                 memory_boundary="Process committed memory; OS RSS is reported independently")
        else:
            # Linux cgroup ownership/delegation is provided explicitly by the
            # operator; never create/reset arbitrary host cgroups or clear caches.
            directory = Path(limits.get("cgroup_directory", "")).resolve()
            if not limits.get("cgroup_directory") or not directory.is_dir() or not (directory / "cgroup.procs").is_file():
                raise ValueError("Linux enforcement requires a pre-created delegated per-worker cgroup_directory")
            if not directory.name.startswith(('bench-','benchmark-')) or (directory/'cgroup.procs').read_text().strip():
                raise ValueError('Delegated worker cgroup must have a benchmark name and contain no other processes')
            memory_max = (directory / "memory.max").read_text().strip()
            allowed = (directory / "cpuset.cpus.effective").read_text().strip()
            if memory_max != str(memory):
                raise ValueError("Delegated cgroup memory.max differs from the frozen budget")
            (directory / "cgroup.procs").write_text(str(pid), encoding="ascii")
            os.sched_setaffinity(pid, cpus)
            if set(os.sched_getaffinity(pid)) != set(cpus):
                raise ValueError("Worker CPU affinity differs after enforcement")
            self.evidence.update(enforced=True, mechanism="Delegated cgroup-v2 memory.max plus sched_setaffinity",
                                 cgroup_directory=str(directory), effective_cpus=allowed,
                                 memory_boundary="cgroup process+descendant memory; OS worker RSS independently reported")

    def close(self):
        if os.name == "nt":
            if self.process:
                self.kernel.CloseHandle(self.process)
                self.process = None
            if self.job:
                self.kernel.CloseHandle(self.job)
                self.job = None
