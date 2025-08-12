from __future__ import annotations

from pathlib import Path
from typing import Dict


def _choose_vmx_for_directory(directory: Path) -> Path | None:
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
    mapping = discover_vms(root)
    return mapping.get(name)


