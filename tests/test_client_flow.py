import sys
import builtins
from unittest.mock import patch

from pathlib import Path as _Path


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_run_client_connect_only(monkeypatch):
    from main import run_client
    import src.cli as cli

    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: ["X", "Y"])
    seq = iter(["X", "현재 상태로 바로 연결"])
    monkeypatch.setattr(cli.VMClient, "choose", lambda items: next(seq))
    monkeypatch.setattr(cli.VMClient, "get_expected_time", lambda self, op: 1.0)
    monkeypatch.setattr(cli.VMClient, "get_snapshot_list", lambda self: ["snap1"])
    monkeypatch.setattr(cli.VMClient, "connect_async", lambda self: "t1")
    monkeypatch.setattr(cli.VMClient, "poll_task", lambda self, tid, expected: {"status": "done", "ip": "1.2.3.4"})
    called = {"rdp": False}
    def _fake_rdp(self, ip):
        assert ip == "1.2.3.4"
        called["rdp"] = True
    monkeypatch.setattr(cli.VMClient, "launch_rdp", _fake_rdp)

    rc = run_client()
    assert rc == 0
    assert called["rdp"] is True


def test_run_client_revert_then_connect(monkeypatch):
    from main import run_client
    import src.cli as cli

    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: ["X", "Y"])
    # choose order: VM first, then snapshot as method
    seq = iter(["X", "snap1"])
    monkeypatch.setattr(cli.VMClient, "choose", lambda items: next(seq))
    monkeypatch.setattr(cli.VMClient, "get_expected_time", lambda self, op: 1.0)
    monkeypatch.setattr(cli.VMClient, "get_snapshot_list", lambda self: ["snap1", "snap2"])
    monkeypatch.setattr(cli.VMClient, "revert_async", lambda self, snap: "rt1")
    def _poll(self, tid, expected):
        if tid == "rt1":
            return {"status": "done", "ip": "1.2.3.4"}
        return {"status": "done", "ip": "2.3.4.5"}
    monkeypatch.setattr(cli.VMClient, "poll_task", _poll)
    monkeypatch.setattr(cli.VMClient, "connect_async", lambda self: "ct1")
    called = {"rdp_ip": None}
    def _fake_rdp(self, ip):
        called["rdp_ip"] = ip
    monkeypatch.setattr(cli.VMClient, "launch_rdp", _fake_rdp)

    rc = run_client()
    assert rc == 0
    assert called["rdp_ip"] == "2.3.4.5"
