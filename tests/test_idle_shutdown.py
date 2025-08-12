from pathlib import Path
import sys
import src.api as api
import src.idle as idle
import src.metrics as metrics
import src.network as network
from fastapi.testclient import TestClient


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))


def test_idle_policy_endpoint_enabled(monkeypatch):
    app = api.create_app()
    client = TestClient(app)
    r = client.get("/idle_policy")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is True
    assert data["mode"] == "soft"
    assert isinstance(data["idle_minutes"], int)
    assert data["idle_minutes"] == 5


def test_watchdog_tick_triggers_shutdown(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(idle, "IDLE_SHUTDOWN_SECONDS", 1, raising=False)

    vmx_file = tmp_path / "A.vmx"
    vmx_file.write_text(".")

    def fake_run_vmrun(args, capture=True, timeout=10):
        if args and args[0] == "list":
            return f"Total running VMs: 1\n{str(vmx_file)}\n"
        raise AssertionError("unexpected vmrun args: %r" % (args,))

    calls = []
    def fake_shutdown(vmx, mode="soft"):
        calls.append((str(vmx), mode))

    monkeypatch.setattr(idle, "run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(idle, "has_active_rdp_connections", lambda vmx, rdp_port=3389: False)
    monkeypatch.setattr(idle, "_shutdown_vm", fake_shutdown)
    idle.IDLE_DB.clear()

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert not calls, "should not shutdown before grace period"

    base = idle.time.time()
    monkeypatch.setattr(idle.time, "time", lambda: base + 5*60 + 1)

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft"))
    assert calls, "shutdown should be called after grace period"
    assert calls[0][0] == str(vmx_file)
    assert calls[0][1] == "soft"


def test_has_active_rdp_connections_prefers_powershell(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "B.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_path, program, *args, timeout=30):
        if "powershell.exe" in program:
            return "YES"
        return ""

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    assert network.has_active_rdp_connections(vmx) is True


def test_has_active_rdp_connections_fallback_quser(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "C.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_path, program, *args, timeout=30):
        if "powershell.exe" in program:
            return ""
        return " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n user                 rdp-tcp#1           2  Active      .  9/1/2025 9:00 AM\n"

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    assert network.has_active_rdp_connections(vmx) is True
