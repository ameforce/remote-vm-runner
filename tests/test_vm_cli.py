import builtins
from unittest.mock import patch

import importlib.util
import sys
from pathlib import Path as _Path


def _load_cli_module():
    root = _Path(__file__).parents[1]
    target = root / "vm-cli.py"
    spec = importlib.util.spec_from_file_location("vm_cli", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vm_cli"] = module
    spec.loader.exec_module(module)
    return module


def test_choose_vm_happy_path(monkeypatch):
    cli = _load_cli_module()
    fake_names = ["B", "A"]
    monkeypatch.setattr(cli, "get_vm_list", lambda: fake_names)

    inputs = iter([""])
    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))

    selected = cli.choose_vm()
    assert selected == "A"
    assert cli.VM_NAME == "A"


def test_get_vm_list_parses_response(monkeypatch):
    cli = _load_cli_module()
    class Resp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"vms": [{"name": "X"}, {"name": "Y"}]}

    monkeypatch.setattr(cli.requests, "get", lambda url, timeout=10: Resp())
    names = cli.get_vm_list()
    assert names == ["X", "Y"]


