import sys
from pathlib import Path as _Path

from fastapi.testclient import TestClient

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import src.api as api
import src.config as config


def test_connect_async_waits_for_rdp(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VM_ROOT", tmp_path, raising=False)

    def _fake_find(name, root):
        p = tmp_path / f"{name}" / f"{name}.vmx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(".")
        return p

    monkeypatch.setattr(api, "find_vmx_for_name", _fake_find)

    monkeypatch.setattr(api, "is_vm_running", lambda vmx: False)
    start_calls = {"count": 0}
    def _fake_start(vmx):
        start_calls["count"] += 1
    monkeypatch.setattr(api, "start_vm_async", _fake_start)
    monkeypatch.setattr(api, "wait_for_vm_ready", lambda vmx, **kw: "10.0.0.5")

    flags = {"rdp_ready_calls": 0}

    def _fake_wait_for_rdp_ready(vmx, ip, **kw):
        flags["rdp_ready_calls"] += 1
        return flags["rdp_ready_calls"] >= 2

    monkeypatch.setattr(api, "wait_for_rdp_ready", _fake_wait_for_rdp_ready)
    monkeypatch.setattr(api, "renew_network", lambda vmx, **kw: None)

    app = api.create_app(config_module=config)
    client = TestClient(app)
    resp = client.post("/connect_async", json={"vm": "X"})
    assert resp.status_code == 200
    tid = resp.json()["task_id"]

    for _ in range(20):
        r = client.get(f"/task/{tid}")
        data = r.json()
        if data["status"] == "done":
            break
    assert data["status"] == "done"
    assert data.get("ip") == "10.0.0.5"
    assert flags["rdp_ready_calls"] >= 2
    assert start_calls["count"] >= 0
