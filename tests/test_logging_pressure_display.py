from __future__ import annotations

import logging
from pathlib import Path

import src.api as api
import src.idle as idle


def _fake_vmrun_no_vms(args, capture=True, timeout=10):
	if args and args[0] == "list":
		return "Total running VMs: 0\n"
	raise AssertionError(f"unexpected vmrun args: {args!r}")


def test_log_displays_instant_mem_pressure(monkeypatch, caplog):
	idle._CPU_OVER_LIMIT_COUNT = 0
	idle.IDLE_DB.clear()

	monkeypatch.setattr(idle, "MIN_AVAILABLE_MEM_GB", 8.0, raising=False)
	monkeypatch.setattr(idle.metrics, "get_host_available_memory_gb", lambda: 1.0)
	monkeypatch.setattr(idle, "CPU_PRESSURE_THRESHOLD_PCT", 95, raising=False)
	monkeypatch.setattr(idle.metrics, "get_host_cpu_percent", lambda: 10.0)
	monkeypatch.setattr(idle, "run_vmrun", _fake_vmrun_no_vms)

	caplog.set_level(logging.INFO, logger=idle.logger.name)
	idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))

	assert idle.LAST_STATUS["mem_pressure"] is True
	assert idle.LAST_STATUS["cpu_pressure"] is False

	assert caplog.records, "expected watchdog log record"
	msg = caplog.records[-1].getMessage()
	assert "pressure=True" in msg
	assert "mem=True" in msg
	assert "cpu=False" in msg


def test_log_displays_instant_cpu_pressure_with_ticks(monkeypatch, caplog):
	idle._CPU_OVER_LIMIT_COUNT = 0
	idle.IDLE_DB.clear()

	monkeypatch.setattr(idle, "MIN_AVAILABLE_MEM_GB", 0.0, raising=False)
	monkeypatch.setattr(idle.metrics, "get_host_available_memory_gb", lambda: 100.0)
	monkeypatch.setattr(idle, "CPU_PRESSURE_THRESHOLD_PCT", 95, raising=False)
	monkeypatch.setattr(idle, "CPU_CONSECUTIVE_TICKS", 3, raising=False)
	monkeypatch.setattr(idle.metrics, "get_host_cpu_percent", lambda: 96.0)
	monkeypatch.setattr(idle, "run_vmrun", _fake_vmrun_no_vms)

	caplog.set_level(logging.INFO, logger=idle.logger.name)
	idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))

	assert idle.LAST_STATUS["mem_pressure"] is False
	assert idle.LAST_STATUS["cpu_pressure"] is True
	assert idle.LAST_STATUS["cpu_over_ticks"] == 1
	assert idle.LAST_STATUS["cpu_required_ticks"] == 3

	assert caplog.records, "expected watchdog log record"
	msg = caplog.records[-1].getMessage()
	assert "pressure=True" in msg
	assert "mem=False" in msg
	assert "cpu=True" in msg
	assert "ticks=1/3" in msg
