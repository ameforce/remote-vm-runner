import sys
from pathlib import Path as _Path

import src.network as network


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_quser_console_active_is_ignored(monkeypatch, tmp_path):
    vmx = tmp_path / "X.vmx"
    vmx.write_text(".")

    monkeypatch.setattr(network, "run_vmrun", lambda args, timeout=8: "running", raising=False)

    console_only = (
        "USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
        ">administrator         console             1  Active      none   9/1/2025 9:00 AM\n"
    )

    def fake_run_in_guest_capture(vmx_arg, *args, **kwargs):
        exe = (args[0] if args else "").lower()
        if exe.endswith("powershell.exe"):
            return ""
        if exe.endswith("quser.exe"):
            return console_only
        if exe.endswith("query.exe"):
            return console_only
        if exe.endswith("qwinsta.exe"):
            return " console                 1  Active       x  y\n"
        if exe.endswith("cmd.exe"):
            return ""
        return ""

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    monkeypatch.setattr(network, "run_script_in_guest_capture", lambda *a, **k: "")
    monkeypatch.setattr(network, "copy_from_guest", lambda *a, **k: False)

    assert network.has_active_rdp_connections(vmx) is False


def test_quser_rdp_active_detected(monkeypatch, tmp_path):
    vmx = tmp_path / "Y.vmx"
    vmx.write_text(".")

    monkeypatch.setattr(network, "run_vmrun", lambda args, timeout=8: "running", raising=False)

    rdp_active = (
        "USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
        "john                  rdp-tcp#12          2  Active      1:23   9/1/2025 9:15 AM\n"
    )

    def fake_run_in_guest_capture(vmx_arg, *args, **kwargs):
        exe = (args[0] if args else "").lower()
        if exe.endswith("powershell.exe"):
            return ""
        if exe.endswith("quser.exe"):
            return rdp_active
        if exe.endswith("query.exe"):
            return rdp_active
        if exe.endswith("qwinsta.exe"):
            return " rdp-tcp#12            2  Active       x  y\n"
        if exe.endswith("cmd.exe"):
            return ""
        return ""

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    monkeypatch.setattr(network, "run_script_in_guest_capture", lambda *a, **k: "")
    monkeypatch.setattr(network, "copy_from_guest", lambda *a, **k: False)

    assert network.has_active_rdp_connections(vmx) is True


def test_fast_detector_ignores_rdpclip_only(monkeypatch, tmp_path):
    vmx = tmp_path / "Z.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_arg, program, *args, timeout=30):
        return ""
    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)
    monkeypatch.setattr(network, "run_vmrun", lambda args, timeout=5: "displayName rdpclip.exe")

    assert network.has_active_rdp_connections_fast(vmx) is False
