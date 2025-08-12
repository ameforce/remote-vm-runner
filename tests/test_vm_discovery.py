from pathlib import Path
import sys
from pathlib import Path as _Path

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SRC))

import src.discovery as mod


def make_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def make_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(".")
    return path


def test_choose_vmx_prefers_matching_stem(tmp_path: Path):
    d = make_dir(tmp_path, "Windows Server 2025")
    make_file(d / "foo.vmx")
    expected = make_file(d / "Windows Server 2025.vmx")
    chosen = mod._choose_vmx_for_directory(d)
    assert chosen == expected


def test_choose_vmx_falls_back_to_first(tmp_path: Path):
    d = make_dir(tmp_path, "WS2025")
    a = make_file(d / "a.vmx")
    b = make_file(d / "b.vmx")
    chosen = mod._choose_vmx_for_directory(d)
    assert chosen in {a, b}


def test_discover_vms_indexes_subdirs(tmp_path: Path):
    d1 = make_dir(tmp_path, "A")
    d2 = make_dir(tmp_path, "B")
    f1 = make_file(d1 / "A.vmx")
    f2 = make_file(d2 / "nested" / "B.vmx")
    mapping = mod.discover_vms(tmp_path)
    assert mapping == {"A": f1, "B": f2}


def test_find_vmx_for_name(tmp_path: Path):
    d = make_dir(tmp_path, "VMXDir")
    f = make_file(d / "VMXDir.vmx")
    out = mod.find_vmx_for_name("VMXDir", tmp_path)
    assert out == f


