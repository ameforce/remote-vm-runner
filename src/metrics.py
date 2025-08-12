from __future__ import annotations

import os
import subprocess
from config import CPU_SAMPLE_DURATION_SEC


def get_host_available_memory_gb() -> float:
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return float(stat.ullAvailPhys) / (1024 ** 3)
        try:
            import psutil  # type: ignore
            return float(psutil.virtual_memory().available) / (1024 ** 3)
        except Exception:
            return 9999.0
    except Exception:
        return 9999.0


def get_host_cpu_percent() -> float:
    try:
        import psutil  # type: ignore
        pct = float(psutil.cpu_percent(interval=CPU_SAMPLE_DURATION_SEC))
        if pct >= 0.0:
            return pct
    except Exception:
        pass

    if os.name == "nt":
        try:
            ps_cmd = (
                "($s=(Get-Counter '" + r"\Processor(_Total)\% Processor Time" + "' -SampleInterval 1 -MaxSamples 1).CounterSamples.CookedValue) | "
                "Measure-Object -Average | ForEach-Object { [int][math]::Round($_.Average) }"
            )
            proc = subprocess.run([
                r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "-NoProfile",
                "-Command",
                ps_cmd,
            ], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                out = proc.stdout.strip()
                if out.isdigit():
                    return float(int(out))
        except Exception:
            pass

        try:
            cmd = ["typeperf", "-sc", "1", r"\Processor(_Total)\% Processor Time"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                if len(lines) >= 3:
                    last = lines[-1]
                    import re as _re
                    m = _re.search(r"([0-9]+[\.,][0-9]+|[0-9]+)$", last)
                    if m:
                        val_str = m.group(1).replace(",", ".")
                        return float(val_str)
        except Exception:
            pass

    return 0.0


