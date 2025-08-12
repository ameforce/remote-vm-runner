"""
Compatibility facade to keep old imports/tests working while delegating logic to
the refactored rvmrunner package. It also exposes legacy symbols expected by tests
and forwards monkeypatched constants into the underlying app via dependency injection.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src layout import: add both project root and src
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

import config as _cfg
import metrics as _metrics
from api import create_app as _create_app
from idle import IDLE_DB as _IDLE_DB  # type: ignore[attr-defined]
from idle import watchdog_tick as _watchdog_tick  # type: ignore[attr-defined]
from idle import _shutdown_vm as _shutdown_vm  # expose for monkeypatch
from models import IdlePolicy, IdleState
from network import has_active_rdp_connections, renew_network
from vmware import (
    fast_wait_for_ip,
    is_vm_running,
    list_snapshots,
    run_in_guest as _run_in_guest,
    run_in_guest_capture as _run_in_guest_capture,
    run_vmrun as _run_vmrun,
    start_vm_async,
    wait_for_vm_ready,
)

# Expose config constants as module-level (tests monkeypatch these on this module)
VMRUN = _cfg.VMRUN
VM_MAP = _cfg.VM_MAP
VM_ROOT = _cfg.VM_ROOT
IP_POLL_INTERVAL = _cfg.IP_POLL_INTERVAL
IP_POLL_TIMEOUT = _cfg.IP_POLL_TIMEOUT
PREFERRED_SUBNETS = _cfg.PREFERRED_SUBNETS
EXCLUDE_SUBNETS = _cfg.EXCLUDE_SUBNETS
GUEST_USER = _cfg.GUEST_USER
GUEST_PASS = _cfg.GUEST_PASS
ENABLE_IDLE_WATCHDOG = _cfg.ENABLE_IDLE_WATCHDOG
IDLE_CHECK_INTERVAL_SEC = _cfg.IDLE_CHECK_INTERVAL_SEC
IDLE_SHUTDOWN_MINUTES = _cfg.IDLE_SHUTDOWN_MINUTES
IDLE_SHUTDOWN_SECONDS = _cfg.IDLE_SHUTDOWN_SECONDS
IDLE_SHUTDOWN_MODE = _cfg.IDLE_SHUTDOWN_MODE
RDP_PORT = _cfg.RDP_PORT
MIN_AVAILABLE_MEM_GB = _cfg.MIN_AVAILABLE_MEM_GB
MAX_SHUTDOWNS_PER_TICK = _cfg.MAX_SHUTDOWNS_PER_TICK
CPU_PRESSURE_THRESHOLD_PCT = _cfg.CPU_PRESSURE_THRESHOLD_PCT
CPU_SAMPLE_DURATION_SEC = _cfg.CPU_SAMPLE_DURATION_SEC
CPU_CONSECUTIVE_TICKS = _cfg.CPU_CONSECUTIVE_TICKS


# Legacy function names used by tests to monkeypatch
def _get_host_available_memory_gb() -> float:  # noqa: N802 (legacy name)
    return _metrics.get_host_available_memory_gb()


def _get_host_cpu_percent() -> float:  # noqa: N802 (legacy name)
    return _metrics.get_host_cpu_percent()


# Build app with this module serving as the config provider
app = _create_app(config_module=sys.modules[__name__])

# Legacy names expected by tests
import time  # re-export for monkeypatching time.time


def _watchdog_tick(policy: IdlePolicy) -> None:  # type: ignore[override]
    # Bridge monkeypatches into the underlying idle module before delegating
    import idle as _idle  # type: ignore

    _idle._shutdown_vm = _shutdown_vm  # type: ignore[attr-defined]
    _idle.has_active_rdp_connections = has_active_rdp_connections  # type: ignore[attr-defined]
    from vmware import run_vmrun as _foo  # noqa: F401

    _idle.run_vmrun = _run_vmrun  # type: ignore[attr-defined]
    # Bridge resource metrics, thresholds and DB into idle module
    _idle.MIN_AVAILABLE_MEM_GB = MIN_AVAILABLE_MEM_GB  # type: ignore[attr-defined]
    _idle.CPU_PRESSURE_THRESHOLD_PCT = CPU_PRESSURE_THRESHOLD_PCT  # type: ignore[attr-defined]
    _idle.CPU_CONSECUTIVE_TICKS = CPU_CONSECUTIVE_TICKS  # type: ignore[attr-defined]
    _idle.IDLE_DB = _IDLE_DB  # type: ignore[attr-defined]
    return _idle.watchdog_tick(policy)


def has_active_rdp_connections(vmx: Path, rdp_port: int = RDP_PORT) -> bool:  # type: ignore[override]
    # Local implementation so tests can monkeypatch `_run_in_guest_capture` on this module
    ps_cmd = (
        f"$c=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue);"
        " if($c){'YES'} else {'NO'}"
    )
    out = _run_in_guest_capture(
        vmx,
        r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        "-NoProfile",
        "-Command",
        ps_cmd,
        timeout=20,
    )
    if out:
        if "YES" in out:
            return True
        if "NO" in out:
            return False

    out = _run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=15)
    if out:
        for line in out.splitlines():
            if line.strip().lower().startswith("username"):
                continue
            if " active " in line.lower():
                return True
    return False


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=495, workers=1)


