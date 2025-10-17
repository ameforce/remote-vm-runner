from __future__ import annotations

import logging
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

from . import metrics
from .config import (
	CPU_CONSECUTIVE_TICKS,
	CPU_PRESSURE_THRESHOLD_PCT,
	MAX_SHUTDOWNS_PER_TICK,
	MIN_AVAILABLE_MEM_GB,
	RDP_PORT,
    RDP_CHECK_CONCURRENCY,
    RDP_CHECK_BATCH_SIZE,
)
from .models import IdlePolicy, IdleState
from .vmrun import run_vmrun


logger = logging.getLogger(__name__)


_CPU_OVER_LIMIT_COUNT = 0
_LAST_CPU_OVER_LIMIT_COUNT = 0
_LAST_MEM_PRESSURE = False
_LAST_CPU_PRESSURE = False
IDLE_DB: dict[str, IdleState] = {}

LAST_STATUS: Dict[str, Any] = {
    "last_tick_at": None,
    "vm_count": 0,
    "pressure": False,
    "mem_pressure": False,
    "cpu_pressure": False,
    "cpu_over_ticks": 0,
    "cpu_required_ticks": 0,
    "available_mem_gb": None,
    "cpu_percent": None,
    "cpu_used_percent": None,
    "cpu_idle_percent": None,
    "stopped_count": 0,
    "last_error": None,
}


def _select_idle_vms_for_stop(candidates: list[Path], limit: int | None = None) -> list[Path]:
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
	max_to_stop = limit if limit is not None else MAX_SHUTDOWNS_PER_TICK
	return [vmx for _, vmx in scored[: max(1, max_to_stop)]]


def _is_pressure_high() -> tuple[bool, float, float]:
	global _CPU_OVER_LIMIT_COUNT
	avail = metrics.get_host_available_memory_gb()
	cpu_pct = metrics.get_host_cpu_percent()
	mem_pressure = avail < MIN_AVAILABLE_MEM_GB
	cpu_pct_for_logic = round(float(cpu_pct), 1)
	if cpu_pct_for_logic >= CPU_PRESSURE_THRESHOLD_PCT:
		_CPU_OVER_LIMIT_COUNT += 1
	else:
		_CPU_OVER_LIMIT_COUNT = 0
	cpu_pressure = _CPU_OVER_LIMIT_COUNT >= max(1, CPU_CONSECUTIVE_TICKS)
	global _LAST_CPU_OVER_LIMIT_COUNT, _LAST_MEM_PRESSURE, _LAST_CPU_PRESSURE
	_LAST_CPU_OVER_LIMIT_COUNT = _CPU_OVER_LIMIT_COUNT
	_LAST_MEM_PRESSURE = bool(mem_pressure)
	_LAST_CPU_PRESSURE = bool(cpu_pressure)
	return ((mem_pressure or cpu_pressure), avail, cpu_pct_for_logic)


def _shutdown_vm(vmx: Path, mode: str = "soft") -> None:
	try:
		if mode == "hard":
			run_vmrun(["stop", str(vmx), "hard"], timeout=30)
		else:
			run_vmrun(["stop", str(vmx), "soft"], timeout=60)
	except Exception:
		pass


def watchdog_tick(policy: IdlePolicy) -> None:
	LAST_STATUS["last_error"] = None
	LAST_STATUS["stopped_count"] = 0
	LAST_STATUS["last_tick_at"] = time.time()
	try:
		running_raw = run_vmrun(["list"], timeout=10)
		lines = [ln.strip() for ln in running_raw.splitlines() if ln.strip()]
		vmx_list: list[Path] = []
		for ln in lines[1:]:
			p = Path(ln)
			if p.suffix.lower() == ".vmx" and p.exists():
				vmx_list.append(p)
	except Exception as exc:
		LAST_STATUS["last_error"] = str(exc)
		vmx_list = []

	LAST_STATUS["vm_count"] = len(vmx_list)

	now = time.time()

	pressure, avail, cpu_pct_used = _is_pressure_high()
	LAST_STATUS["pressure"] = pressure
	LAST_STATUS["cpu_over_ticks"] = int(_LAST_CPU_OVER_LIMIT_COUNT)
	LAST_STATUS["cpu_required_ticks"] = int(max(1, CPU_CONSECUTIVE_TICKS))
	LAST_STATUS["available_mem_gb"] = float(avail)
	LAST_STATUS["cpu_used_percent"] = float(cpu_pct_used)
	LAST_STATUS["cpu_percent"] = float(cpu_pct_used)
	LAST_STATUS["cpu_idle_percent"] = max(0.0, 100.0 - float(cpu_pct_used))
	LAST_STATUS["interval_sec"] = int(getattr(policy, "check_interval_sec", 0) or 0)

	LAST_STATUS["mem_pressure"] = bool(float(LAST_STATUS["available_mem_gb"]) < float(MIN_AVAILABLE_MEM_GB))
	LAST_STATUS["cpu_pressure"] = bool(float(LAST_STATUS["cpu_used_percent"]) >= float(CPU_PRESSURE_THRESHOLD_PCT))

	active_map: dict[Path, bool] = {}
	if vmx_list:
		if RDP_CHECK_BATCH_SIZE > 0:
			vmx_targets = vmx_list[: RDP_CHECK_BATCH_SIZE]
		else:
			vmx_targets = vmx_list
		from .network import has_active_rdp_connections_tcp as _tcp
		checker = _tcp

		if checker is not None:
			max_workers = max(1, min(RDP_CHECK_CONCURRENCY, len(vmx_targets)))
			with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rdpchk") as pool:
				future_to_vmx = {pool.submit(checker, vmx, RDP_PORT): vmx for vmx in vmx_targets}
				for fut in as_completed(future_to_vmx):
					vmx = future_to_vmx[fut]
					try:
						active = bool(fut.result())
					except Exception:
						active = False
					active_map[vmx] = active
					key = str(vmx)
					name = vmx.parent.name
					if active:
						IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)

	victims_total = 0
	for vmx in vmx_list:
		name = vmx.parent.name
		active = active_map.get(vmx, False)
		key = str(vmx)
		state = IDLE_DB.get(key)
		if active:
			IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)
			continue

		if state is None or state.last_active_ts is None:
			IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now)
			continue

		if not state.shutting_down:
			if getattr(policy, "only_on_pressure", False) and not pressure:
				continue
			if not pressure:
				continue
			if _LAST_CPU_OVER_LIMIT_COUNT < max(1, CPU_CONSECUTIVE_TICKS):
				continue
			candidates = [v for v in vmx_list if not active_map.get(v, False)]
			per_tick_limit = 1 if pressure else None
			to_stop = _select_idle_vms_for_stop(candidates, limit=per_tick_limit)
			for victim in to_stop:
				key2 = str(victim)
				s2 = IDLE_DB.get(key2) or IdleState(vm=victim.parent.name, vmx=str(victim), last_active_ts=now)
				IDLE_DB[key2] = IdleState(vm=s2.vm, vmx=s2.vmx, last_active_ts=s2.last_active_ts, shutting_down=True)
				if pressure:
					logger.warning(
						"watchdog: stopping VM due to %s – vm=%s vmx=%s mem_avail=%.2fGB cpu_used=%.1f%%",
						"idle+pressure",
						victim.parent.name,
						victim,
						LAST_STATUS.get("available_mem_gb") or -1.0,
						LAST_STATUS.get("cpu_used_percent") or -1.0,
					)
				else:
					logger.info(
						"watchdog: stopping VM due to %s – vm=%s vmx=%s mem_avail=%.2fGB cpu_used=%.1f%%",
						"idle",
						victim.parent.name,
						victim,
						LAST_STATUS.get("available_mem_gb") or -1.0,
						LAST_STATUS.get("cpu_used_percent") or -1.0,
					)
				_shutdown_vm(victim, mode=policy.mode)
			victims_total += len(to_stop)
			break

	LAST_STATUS["stopped_count"] = victims_total

	stop_reason = "none"
	if victims_total > 0:
		if LAST_STATUS.get("pressure"):
			stop_reason = "idle+pressure"
		else:
			stop_reason = "idle"
	elif LAST_STATUS.get("pressure"):
		stop_reason = "pressure-only"

	inst = bool(LAST_STATUS["mem_pressure"]) or bool(LAST_STATUS["cpu_pressure"])
	msg = (
		f"watchdog: vms={LAST_STATUS['vm_count']} | mem_avail={LAST_STATUS['available_mem_gb']:.2f}GB | "
		f"cpu_avail={LAST_STATUS['cpu_idle_percent']:.1f}% | "
		f"pressure={inst} "
		f"(mem={bool(LAST_STATUS['mem_pressure'])} cpu={bool(LAST_STATUS['cpu_pressure'])} "
		f"ticks={LAST_STATUS['cpu_over_ticks']}/{LAST_STATUS['cpu_required_ticks']})"
	)
	if victims_total > 0 or inst:
		logger.warning(msg)
	else:
		logger.info(msg)
