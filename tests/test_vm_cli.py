import builtins
from unittest.mock import patch
import sys
from pathlib import Path as _Path

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

import src.cli as cli


def test_choose_vm_happy_path(monkeypatch):
    fake_names = ["B", "A"]
    monkeypatch.setattr(cli.VMClient, "get_vm_list", lambda self: fake_names)

    inputs = iter([""])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))

    selected = cli.VMClient.choose(["A", "B"])  # choose uses input('') mock -> returns first
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


