import sys
import src.cli as cli
import builtins
from unittest.mock import patch
from pathlib import Path as _Path


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))


def test_choose_vm_happy_path(monkeypatch):
    fake_names = ["B", "A"]
    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: fake_names)

    inputs = iter([""])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))

    selected = cli.VMClient.choose(["A", "B"])
    assert selected == "A"


def test_get_vm_list_parses_response(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"vms": [{"name": "X"}, {"name": "Y"}]}

    monkeypatch.setattr(cli.requests, "get", lambda url, timeout=10: Resp())
    names = cli.VMClient("http://x").get_vm_list()
    assert names == ["X", "Y"]


def test_format_vm_list_with_rdp_labels(monkeypatch):
    names = ["A", "B", "C"]

    monkeypatch.setattr(cli.VMClient, "get_rdp_active", lambda self, vm: vm in {"A", "C"})

    def _fetch(vm):
        return {"A": ["10.0.0.1"], "B": [], "C": ["fe80::1"]}.get(vm, [])

    labeled = cli.VMClient.format_vm_list_with_rdp(names, lambda vm: vm in {"A", "C"}, _fetch)
    assert labeled[0].startswith("A [RDP: 10.0.0.1]")
    assert labeled[1] == "B"
    assert labeled[2].startswith("C [RDP: fe80::1]")


def test_run_client_labels_error_on_rdp_used_failure(monkeypatch):
    import main as entry
    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: ["X", "Y"])
    def fake_rdp_used(self, vm):
        if vm == "X":
            raise RuntimeError("boom")
        return False, []
    monkeypatch.setattr(cli.VMClient, "get_rdp_used", fake_rdp_used)

    seq = iter([""])
    monkeypatch.setattr(cli.VMClient, "choose", lambda items: next(seq))
    monkeypatch.setattr(cli.VMClient, "get_snapshot_list", lambda self: [])
    monkeypatch.setattr(cli.VMClient, "get_expected_time", lambda self, op: 0)
    monkeypatch.setattr(cli.VMClient, "connect_async", lambda self: "t1")
    monkeypatch.setattr(cli.VMClient, "poll_task", lambda self, tid, expected: {"status": "done", "ip": "1.2.3.4"})
    monkeypatch.setattr(cli.VMClient, "launch_rdp", lambda self, ip: None)

    import requests as _req
    monkeypatch.setattr(cli.requests, "get", lambda *a, **k: (_ for _ in ()).throw(_req.RequestException("stub")))

    import io, sys as _sys
    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        rc = entry.run_client()
    finally:
        _sys.stdout = old
    out = buf.getvalue()
    assert rc == 0
    assert "X  (사용자: 확인 에러 발생)" in out
    assert "Y  (사용자: 없음)" in out
    