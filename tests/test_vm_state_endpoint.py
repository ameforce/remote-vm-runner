import sys
from pathlib import Path as _Path

from fastapi.testclient import TestClient

import src.api as api
import src.config as config


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_vm_state_running(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VM_ROOT", tmp_path, raising=False)
    from src.discovery import find_vmx_for_name as _orig_find

    def _fake_find(name, root):
        return tmp_path / f"{name}" / f"{name}.vmx"

    monkeypatch.setattr(api, "find_vmx_for_name", _fake_find)
    monkeypatch.setattr(api, "is_vm_running", lambda vmx: True)

    app = api.create_app(config_module=config)
    client = TestClient(app)
    resp = client.get("/vm_state", params={"vm": "X"})
    assert resp.status_code == 200
    assert resp.json()["running"] is True


def test_vm_state_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VM_ROOT", tmp_path, raising=False)

    def _fake_find(name, root):
        return tmp_path / f"{name}" / f"{name}.vmx"

    monkeypatch.setattr(api, "find_vmx_for_name", _fake_find)
    monkeypatch.setattr(api, "is_vm_running", lambda vmx: False)

    app = api.create_app(config_module=config)
    client = TestClient(app)
    resp = client.get("/vm_state", params={"vm": "Y"})
    assert resp.status_code == 200
    assert resp.json()["running"] is False


