from pathlib import Path
import importlib.util
import sys
from fastapi.testclient import TestClient


def _load_api_module():
    root = Path(__file__).parents[1]
    target = root / "qa-vm-api.py"
    spec = importlib.util.spec_from_file_location("qa_vm_api", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_vm_api"] = module
    spec.loader.exec_module(module)
    return module


def test_idle_policy_endpoint_enabled(monkeypatch):
    api = _load_api_module()
    client = TestClient(api.app)
    r = client.get("/idle_policy")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["mode"] == "soft"
    assert isinstance(data["idle_minutes"], int)
    assert data["idle_minutes"] == 5


def test_watchdog_tick_triggers_shutdown(monkeypatch, tmp_path: Path):
    api = _load_api_module()
    monkeypatch.setattr(api, "IDLE_SHUTDOWN_SECONDS", 1, raising=False)

    vmx_file = tmp_path / "A.vmx"
    vmx_file.write_text(".")

    def fake_run_vmrun(args, capture=True, timeout=10):
        if args and args[0] == "list":
            return f"Total running VMs: 1\n{str(vmx_file)}\n"
        raise AssertionError("unexpected vmrun args: %r" % (args,))

    calls = []
    def fake_shutdown(vmx, mode="soft"):
        calls.append((str(vmx), mode))

    monkeypatch.setattr(api, "_run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(api, "has_active_rdp_connections", lambda vmx, rdp_port=3389: False)
    monkeypatch.setattr(api, "_shutdown_vm", fake_shutdown)
    api._IDLE_DB.clear()

    # First tick should not shutdown due to 5-minute grace
    api._watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert not calls, "should not shutdown before grace period"

    # Simulate passage of time beyond threshold
    base = api.time.time()
    monkeypatch.setattr(api.time, "time", lambda: base + 5*60 + 1)

    api._watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert calls, "shutdown should be called after grace period"
    assert calls[0][0] == str(vmx_file)
    assert calls[0][1] == "soft"


def test_has_active_rdp_connections_prefers_powershell(monkeypatch, tmp_path: Path):
    api = _load_api_module()
    vmx = tmp_path / "B.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_path, program, *args, timeout=30):
        if "powershell.exe" in program:
            return "YES"
        return ""

    monkeypatch.setattr(api, "_run_in_guest_capture", fake_run_in_guest_capture)
    assert api.has_active_rdp_connections(vmx) is True


def test_has_active_rdp_connections_fallback_quser(monkeypatch, tmp_path: Path):
    api = _load_api_module()
    vmx = tmp_path / "C.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_path, program, *args, timeout=30):
        if "powershell.exe" in program:
            return ""
        return " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n user                 rdp-tcp#1           2  Active      .  9/1/2025 9:00 AM\n"

    monkeypatch.setattr(api, "_run_in_guest_capture", fake_run_in_guest_capture)
    assert api.has_active_rdp_connections(vmx) is True


