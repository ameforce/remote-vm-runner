from __future__ import annotations

import time
from pathlib import Path

from . import metrics
from .config import (
    CPU_CONSECUTIVE_TICKS,
    CPU_PRESSURE_THRESHOLD_PCT,
    IDLE_SHUTDOWN_MINUTES,
    MAX_SHUTDOWNS_PER_TICK,
    MIN_AVAILABLE_MEM_GB,
    RDP_PORT,
)
from .models import IdlePolicy, IdleState
from .network import has_active_rdp_connections
from .vmrun import run_vmrun


_CPU_OVER_LIMIT_COUNT = 0
IDLE_DB: dict[str, IdleState] = {}


def _select_idle_vms_for_stop(candidates: list[Path]) -> list[Path]:
    if not candidates:
        return []
    scored: list[tuple[float, Path]] = []
    now = time.time()
    for vmx in candidates:
        key = str(vmx)
        st = IDLE_DB.get(key)
        last = st.last_active_ts if st and st.last_active_ts is not None else 0.0
        scored.append((last, vmx))
    scored.sort(key=lambda x: (x[0], str(x[1]).lower()))
    return [vmx for _, vmx in scored[: max(1, MAX_SHUTDOWNS_PER_TICK)]]


def _is_pressure_high() -> tuple[bool, float, float]:
    global _CPU_OVER_LIMIT_COUNT
    avail = metrics.get_host_available_memory_gb()
    cpu_pct = metrics.get_host_cpu_percent()
    mem_pressure = avail < MIN_AVAILABLE_MEM_GB
    if cpu_pct >= CPU_PRESSURE_THRESHOLD_PCT:
        _CPU_OVER_LIMIT_COUNT += 1
    else:
        _CPU_OVER_LIMIT_COUNT = 0
    cpu_pressure = _CPU_OVER_LIMIT_COUNT >= max(1, CPU_CONSECUTIVE_TICKS)
    return ((mem_pressure or cpu_pressure), avail, cpu_pct)


def _shutdown_vm(vmx: Path, mode: str = "soft") -> None:
    try:
        if mode == "hard":
            run_vmrun(["stop", str(vmx), "hard"], timeout=30)
        else:
            run_vmrun(["stop", str(vmx), "soft"], timeout=60)
    except Exception:
        pass


def watchdog_tick(policy: IdlePolicy) -> None:
    try:
        running_raw = run_vmrun(["list"], timeout=10)
        lines = [ln.strip() for ln in running_raw.splitlines() if ln.strip()]
        vmx_list: list[Path] = []
        for ln in lines[1:]:
            p = Path(ln)
            if p.suffix.lower() == ".vmx" and p.exists():
                vmx_list.append(p)
    except Exception:
        vmx_list = []

    now = time.time()
    threshold_seconds = max(0, int(policy.idle_minutes) * 60)

    pressure, avail, cpu_pct = _is_pressure_high()
    for vmx in vmx_list:
        active = has_active_rdp_connections(vmx, rdp_port=RDP_PORT)
        key = str(vmx)
        name = vmx.parent.name
        if active:
            IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)

    for vmx in vmx_list:
        name = vmx.parent.name
        active = has_active_rdp_connections(vmx, rdp_port=RDP_PORT)
        key = str(vmx)
        state = IDLE_DB.get(key)
        if active:
            IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)
            continue

        if threshold_seconds == 0:
            continue

        if state is None or state.last_active_ts is None:
            IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now)
            continue

        idle_secs = now - state.last_active_ts
        if idle_secs >= threshold_seconds and not state.shutting_down:
            candidates = [v for v in vmx_list if not has_active_rdp_connections(v, rdp_port=RDP_PORT)]
            to_stop = _select_idle_vms_for_stop(candidates)
            for victim in to_stop:
                key2 = str(victim)
                s2 = IDLE_DB.get(key2) or IdleState(vm=victim.parent.name, vmx=str(victim), last_active_ts=now)
                IDLE_DB[key2] = IdleState(vm=s2.vm, vmx=s2.vmx, last_active_ts=s2.last_active_ts, shutting_down=True)
                _shutdown_vm(victim, mode=policy.mode)
            break
