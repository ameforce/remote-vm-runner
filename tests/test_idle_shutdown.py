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


def test_fast_detector_uses_lightweight_tools(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "D.vmx"
    vmx.write_text(".")

    calls = {"query": 0, "qwinsta": 0, "list": 0}

    def fake_run_in_guest_capture(vmx_path, program, *args, timeout=30):
        if "query.exe" in program:
            calls["query"] += 1
            return " user rdp-tcp#1 2 Active "
        if "qwinsta.exe" in program:
            calls["qwinsta"] += 1
            return "rdp-tcp 1 Active"
        return ""

    def fake_vmrun(args, timeout=5):
        calls["list"] += 1
        return "displayName rdpclip.exe"

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    monkeypatch.setattr(network, "run_vmrun", fake_vmrun)

    assert network.has_active_rdp_connections_fast(vmx) is True
    assert calls["query"] >= 1


def test_tcp_detector_is_passive(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "E.vmx"
    vmx.write_text(".")

    def fake_vmrun(args, timeout=6):
        assert args[0] == "getGuestIPAddress"
        return "1.2.3.4"

    class Sock:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def close(self):
            pass

    def fake_create_connection(addr, timeout=1.0):
        assert addr == ("1.2.3.4", 3389)
        return Sock()

    monkeypatch.setattr(network, "run_vmrun", fake_vmrun)
    monkeypatch.setattr(network.socket, "create_connection", fake_create_connection)

    assert network.has_active_rdp_connections_tcp(vmx) is True


def test_idle_shutdown_only_on_pressure_skips_when_no_pressure(monkeypatch, tmp_path: Path):
    vmx_file = tmp_path / "P.vmx"
    vmx_file.write_text(".")

    def fake_run_vmrun(args, capture=True, timeout=10):
        if args and args[0] == "list":
            return f"Total running VMs: 1\n{str(vmx_file)}\n"
        raise AssertionError("unexpected vmrun args: %r" % (args,))

    calls = []
    def fake_shutdown(vmx, mode="soft"):
        calls.append((str(vmx), mode))

    # Host not under pressure
    monkeypatch.setattr(idle, "_is_pressure_high", lambda: (False, 10.0, 10.0))
    monkeypatch.setattr(idle, "run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(idle, "_shutdown_vm", fake_shutdown)
    monkeypatch.setattr(idle, "has_active_rdp_connections", lambda vmx, rdp_port=3389: (_ for _ in ()).throw(AssertionError("should not be called")))
    idle.IDLE_DB.clear()

    base = idle.time.time()
    idle.IDLE_DB[str(vmx_file)] = idle.IdleState(vm="P", vmx=str(vmx_file), last_active_ts=base - 5*60 - 5)
    monkeypatch.setattr(idle.time, "time", lambda: base)

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft", only_on_pressure=True))

    assert not calls, "should not shutdown when only_on_pressure=True and no pressure"


def test_idle_shutdown_only_on_pressure_triggers_on_pressure(monkeypatch, tmp_path: Path):
    vmx_file = tmp_path / "Q.vmx"
    vmx_file.write_text(".")

    def fake_run_vmrun(args, capture=True, timeout=10):
        if args and args[0] == "list":
            return f"Total running VMs: 1\n{str(vmx_file)}\n"
        raise AssertionError("unexpected vmrun args: %r" % (args,))

    calls = []
    def fake_shutdown(vmx, mode="soft"):
        calls.append((str(vmx), mode))

    # Host under pressure
    monkeypatch.setattr(idle, "_is_pressure_high", lambda: (True, 1.0, 97.0))
    monkeypatch.setattr(idle, "run_vmrun", fake_run_vmrun)
    monkeypatch.setattr(idle, "_shutdown_vm", fake_shutdown)
    monkeypatch.setattr(idle, "has_active_rdp_connections", lambda vmx, rdp_port=3389: False)
    idle.IDLE_DB.clear()

    base = idle.time.time()
    idle.IDLE_DB[str(vmx_file)] = idle.IdleState(vm="Q", vmx=str(vmx_file), last_active_ts=base - 5*60 - 5)
    monkeypatch.setattr(idle.time, "time", lambda: base)

    idle.watchdog_tick(api.IdlePolicy(enabled=True, idle_minutes=5, check_interval_sec=1, mode="soft", only_on_pressure=True))

    assert calls, "should shutdown when only_on_pressure=True and pressure present"
    assert calls[0][0] == str(vmx_file)