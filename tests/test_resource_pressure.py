from pathlib import Path
import importlib.util
import sys


def _load_api_module():
    root = Path(__file__).parents[1]
    target = root / "qa-vm-api.py"
    spec = importlib.util.spec_from_file_location("qa_vm_api", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_vm_api"] = module
    spec.loader.exec_module(module)
    return module


def test_pressure_reclaims_idle(monkeypatch, tmp_path: Path):
    api = _load_api_module()
    # Force pressure high (memory or CPU)
    monkeypatch.setattr(api, "MIN_AVAILABLE_MEM_GB", 8.0, raising=False)
    monkeypatch.setattr(api, "_get_host_available_memory_gb", lambda: 1.0)
    monkeypatch.setattr(api, "CPU_PRESSURE_THRESHOLD_PCT", 90, raising=False)
    monkeypatch.setattr(api, "_get_host_cpu_percent", lambda: 99.0)
    monkeypatch.setattr(api, "MAX_SHUTDOWNS_PER_TICK", 2, raising=False)

    # Prepare two running idle VMs and one active
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

    # active detection: last one is active
    def fake_has_active(vmx, rdp_port=3389):
        return str(vmx) == str(vmx_active)

    monkeypatch.setattr(api, "_run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(api, "_shutdown_vm", fake_shutdown)
    monkeypatch.setattr(api, "has_active_rdp_connections", fake_has_active)
    api._IDLE_DB.clear()

    # Simulate last_active_ts older than threshold for idles
    now = api.time.time()
    api._IDLE_DB[str(vmx_idle1)] = api.IdleState(vm="idle1", vmx=str(vmx_idle1), last_active_ts=now - 600)
    api._IDLE_DB[str(vmx_idle2)] = api.IdleState(vm="idle2", vmx=str(vmx_idle2), last_active_ts=now - 601)
    api._IDLE_DB[str(vmx_active)] = api.IdleState(vm="active", vmx=str(vmx_active), last_active_ts=now)

    # Tick with 5-minute idle policy
    api._watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))

    # Should stop up to MAX_SHUTDOWNS_PER_TICK idle VMs, not the active one
    assert calls
    assert str(vmx_active) not in calls
    assert set(calls).issubset({str(vmx_idle1), str(vmx_idle2)})


