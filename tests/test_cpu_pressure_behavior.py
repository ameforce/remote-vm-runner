from __future__ import annotations

from pathlib import Path

import src.api as api
import src.idle as idle


def _fake_vmrun_no_vms(args, capture=True, timeout=10):

    if args and args[0] == "list":
        return "Total running VMs: 0\n"
    raise AssertionError(f"unexpected vmrun args: {args!r}")


def test_cpu_pressure_triggers_on_rounded_threshold(monkeypatch):

    # reset global counters between tests
    idle._CPU_OVER_LIMIT_COUNT = 0

    monkeypatch.setattr(idle, "MIN_AVAILABLE_MEM_GB", 0.0, raising=False)
    monkeypatch.setattr(idle, "CPU_PRESSURE_THRESHOLD_PCT", 95, raising=False)
    monkeypatch.setattr(idle, "CPU_CONSECUTIVE_TICKS", 1, raising=False)
    monkeypatch.setattr(idle.metrics, "get_host_available_memory_gb", lambda: 100.0)
    monkeypatch.setattr(idle.metrics, "get_host_cpu_percent", lambda: 94.95)
    monkeypatch.setattr(idle, "run_vmrun", _fake_vmrun_no_vms)

    idle.IDLE_DB.clear()

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))

    assert idle.LAST_STATUS["cpu_used_percent"] == 95.0
    assert idle.LAST_STATUS["pressure"] is True
    assert idle.LAST_STATUS["mem_pressure"] is False
    assert idle.LAST_STATUS["cpu_pressure"] is True
    assert idle.LAST_STATUS["cpu_over_ticks"] == 1
    assert idle.LAST_STATUS["cpu_required_ticks"] == 1


def test_cpu_pressure_requires_consecutive_ticks(monkeypatch, tmp_path: Path):

    # reset global counters between tests
    idle._CPU_OVER_LIMIT_COUNT = 0

    monkeypatch.setattr(idle, "MIN_AVAILABLE_MEM_GB", 0.0, raising=False)
    monkeypatch.setattr(idle, "CPU_PRESSURE_THRESHOLD_PCT", 95, raising=False)
    monkeypatch.setattr(idle, "CPU_CONSECUTIVE_TICKS", 2, raising=False)
    monkeypatch.setattr(idle.metrics, "get_host_available_memory_gb", lambda: 100.0)
    monkeypatch.setattr(idle, "run_vmrun", _fake_vmrun_no_vms)

    # sequence: above threshold, then below -> should NOT trigger pressure
    seq = [96.0, 94.0, 96.0, 96.0]

    def seq_cpu():
        return seq.pop(0)

    monkeypatch.setattr(idle.metrics, "get_host_cpu_percent", seq_cpu)

    idle.IDLE_DB.clear()

    # 1st tick: 96.0 -> over_ticks = 1/2 -> not yet
    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert idle.LAST_STATUS["pressure"] is False
    assert idle.LAST_STATUS["cpu_over_ticks"] == 1

    # 2nd tick: 94.0 -> reset -> not pressured
    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert idle.LAST_STATUS["pressure"] is False
    assert idle.LAST_STATUS["cpu_over_ticks"] == 0

    # 3rd tick: 96.0 -> over_ticks = 1/2 -> not yet
    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert idle.LAST_STATUS["pressure"] is False
    assert idle.LAST_STATUS["cpu_over_ticks"] == 1

    # 4th tick: 96.0 -> over_ticks = 2/2 -> pressured
    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert idle.LAST_STATUS["pressure"] is True
    assert idle.LAST_STATUS["cpu_pressure"] is True
    assert idle.LAST_STATUS["cpu_over_ticks"] == 2
    assert idle.LAST_STATUS["cpu_required_ticks"] == 2


