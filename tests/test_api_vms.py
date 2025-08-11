from pathlib import Path
from fastapi.testclient import TestClient
import importlib.util
import sys
from pathlib import Path as _Path


def _load_api_module():
    root = _Path(__file__).parents[1]
    target = root / "qa-vm-api.py"
    spec = importlib.util.spec_from_file_location("qa_vm_api", target)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["qa_vm_api"] = module
    spec.loader.exec_module(module)
    return module


def test_list_vms_endpoint(monkeypatch, tmp_path: Path):
    # Prepare fake VM root
    root = tmp_path / "VMware"
    (root / "Windows Server 2025").mkdir(parents=True)
    (root / "Windows Server 2025" / "Windows Server 2025.vmx").write_text(".")
    (root / "MyVM").mkdir()
    (root / "MyVM" / "nested").mkdir()
    (root / "MyVM" / "nested" / "Another.vmx").write_text(".")

    api = _load_api_module()
    # Point API to fake root
    monkeypatch.setattr(api, "VM_ROOT", root)
    client = TestClient(api.app)
    resp = client.get("/vms")
    assert resp.status_code == 200
    data = resp.json()
    names = {item["name"] for item in data["vms"]}
    assert names == {"Windows Server 2025", "MyVM"}
    assert data["root"] == str(root)


