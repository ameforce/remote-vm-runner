import sys
from pathlib import Path as _Path

from main import run_client
import src.cli as cli
from src.errors import ExitCode

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_client_selects_warm_or_cold(monkeypatch):

    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: ["X"]) 
    seq = iter(["X", "현재 상태로 바로 연결"])
    monkeypatch.setattr(cli.VMClient, "choose", lambda items: next(seq))
    monkeypatch.setattr(cli.VMClient, "get_vm_state", lambda self: True)
    called = {"op": None}
    def _get_et(self, op):
        called["op"] = op
        return 1.0
    monkeypatch.setattr(cli.VMClient, "get_expected_time", _get_et)
    monkeypatch.setattr(cli.VMClient, "get_snapshot_list", lambda self: [])
    monkeypatch.setattr(cli.VMClient, "connect_async", lambda self: "t1")
    monkeypatch.setattr(cli.VMClient, "poll_task", lambda self, tid, expected: {"status": "done", "ip": "1.1.1.1"})
    monkeypatch.setattr(cli.VMClient, "launch_rdp", lambda self, ip: None)

    rc = run_client()
    assert rc == int(ExitCode.SUCCESS)
    assert called["op"] in {"connect_warm", "connect"}
