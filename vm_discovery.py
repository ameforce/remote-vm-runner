from __future__ import annotations

from pathlib import Path
from typing import Dict


def _choose_vmx_for_directory(directory: Path) -> Path | None:
    """Select a .vmx file for the given directory.

    Preference order:
    1) A .vmx whose stem matches the directory name (case/space-insensitive)
    2) The first .vmx found in a deterministic order
    """
    if not directory.is_dir():
        return None

    dir_key = directory.name.lower().replace(" ", "")

    candidates: list[Path] = sorted(directory.rglob("*.vmx"))
    if not candidates:
        return None

    for vmx in candidates:
        stem_key = vmx.stem.lower().replace(" ", "")
        if stem_key == dir_key:
            return vmx

    return candidates[0]


def discover_vms(root: Path) -> Dict[str, Path]:
    """Discover VMs under `root`.

    Returns a mapping: { directory_name: vmx_path }
    Only immediate subdirectories of `root` are considered as VM names.
    A .vmx is searched recursively within each subdirectory.
    """
    mapping: dict[str, Path] = {}
    if not root.exists() or not root.is_dir():
        return mapping

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        chosen = _choose_vmx_for_directory(entry)
        if chosen is not None:
            mapping[entry.name] = chosen
    return mapping


def find_vmx_for_name(name: str, root: Path) -> Path | None:
    """Find the .vmx Path for the VM whose directory name equals `name`.

    Returns None when not found.
    """
    mapping = discover_vms(root)
    return mapping.get(name)


