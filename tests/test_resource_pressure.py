from pathlib import Path
import sys
import src.api as api
import src.idle as idle
import src.models as models


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))


def test_pressure_reclaims_idle(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(idle, "MIN_AVAILABLE_MEM_GB", 8.0, raising=False)
    monkeypatch.setattr(idle.metrics, "get_host_available_memory_gb", lambda: 1.0)
    monkeypatch.setattr(idle, "CPU_PRESSURE_THRESHOLD_PCT", 90, raising=False)
    monkeypatch.setattr(idle.metrics, "get_host_cpu_percent", lambda: 99.0)
    monkeypatch.setattr(idle, "MAX_SHUTDOWNS_PER_TICK", 2, raising=False)

    vmx_idle1 = tmp_path / "idle1.vmx"; vmx_idle1.write_text(".")
    vmx_idle2 = tmp_path / "idle2.vmx"; vmx_idle2.write_text(".")
    vmx_active = tmp_path / "active.vmx"; vmx_active.write_text(".")

    def fake_run_vmrun(args, capture=True, timeout=10):
        if args and args[0] == "list":
            return f"Total running VMs: 3\n{vmx_idle1}\n{vmx_idle2}\n{vmx_active}\n"
        raise AssertionError("unexpected vmrun args: %r" % (args,))

    calls = []
    def fake_shutdown(vmx, mode="soft"):
        calls.append(str(vmx))

    def fake_has_active(vmx, rdp_port=3389):
        return str(vmx) == str(vmx_active)

    monkeypatch.setattr(idle, "run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(idle, "_shutdown_vm", fake_shutdown)
    monkeypatch.setattr(idle, "has_active_rdp_connections", fake_has_active)
    idle.IDLE_DB.clear()

    now = idle.time.time()
    idle.IDLE_DB[str(vmx_idle1)] = models.IdleState(vm="idle1", vmx=str(vmx_idle1), last_active_ts=now - 600)
    idle.IDLE_DB[str(vmx_idle2)] = models.IdleState(vm="idle2", vmx=str(vmx_idle2), last_active_ts=now - 601)
    idle.IDLE_DB[str(vmx_active)] = models.IdleState(vm="active", vmx=str(vmx_active), last_active_ts=now)

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))

    assert calls
    assert str(vmx_active) not in calls
    assert set(calls).issubset({str(vmx_idle1), str(vmx_idle2)})
    assert len(calls) == 1
