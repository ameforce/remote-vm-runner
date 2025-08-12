from pathlib import Path
from fastapi.testclient import TestClient
import sys
import src.api as api
import src.config as config
from pathlib import Path as _Path


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))


def test_list_vms_endpoint(monkeypatch, tmp_path: Path):
    root = tmp_path / "VMware"
    (root / "Windows Server 2025").mkdir(parents=True)
    (root / "Windows Server 2025" / "Windows Server 2025.vmx").write_text(".")
    (root / "MyVM").mkdir()
    (root / "MyVM" / "nested").mkdir()
    (root / "MyVM" / "nested" / "Another.vmx").write_text(".")

    monkeypatch.setattr(config, "VM_ROOT", root, raising=False)
    app = api.create_app(config_module=config)
    client = TestClient(app)
    resp = client.get("/vms")
    assert resp.status_code == 200
    data = resp.json()
    names = {item["name"] for item in data["vms"]}
    assert names == {"Windows Server 2025", "MyVM"}
    assert data["root"] == str(root)
