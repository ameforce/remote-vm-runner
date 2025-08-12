from pathlib import Path
import sys

import src.network as network


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))


def test_has_active_rdp_connections_assume_active_on_failure(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "A.vmx"
    vmx.write_text(".")

    monkeypatch.setattr(network, "ASSUME_ACTIVE_ON_FAILURE", True, raising=False)

    calls = {"ps": 0, "quser": 0}

    def fake_run_in_guest_capture(vmx_arg, *args, **kwargs):
        exe = args[0] if args else ""
        if "powershell.exe" in exe.lower():
            calls["ps"] += 1
            raise RuntimeError("simulated PS failure")
        if "quser.exe" in exe.lower():
            calls["quser"] += 1
            return ""
        raise AssertionError("unexpected command")

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)

    assert network.has_active_rdp_connections(vmx) is True
    assert calls["ps"] == 1
    assert calls["quser"] == 1


def test_has_active_rdp_connections_negative_when_definitive(monkeypatch, tmp_path: Path):
    vmx = tmp_path / "B.vmx"
    vmx.write_text(".")

    def fake_run_in_guest_capture(vmx_arg, *args, **kwargs):
        exe = args[0] if args else ""
        if "powershell.exe" in exe.lower():
            return "NO"
        raise AssertionError("unexpected call order")

    monkeypatch.setattr(network, "run_in_guest_capture", fake_run_in_guest_capture)

    assert network.has_active_rdp_connections(vmx) is False
