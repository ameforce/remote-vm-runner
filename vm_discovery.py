"""
Backward-compatible shim to the new package API. Tests importing this module
will continue to work while delegating to rvmrunner.discovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
import sys

# Ensure src layout import
from pathlib import Path as _Path
BASE_DIR = _Path(__file__).parent
ROOT_DIR = BASE_DIR
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

from discovery import (
    _choose_vmx_for_directory as _choose_vmx_for_directory,  # re-export
    discover_vms as discover_vms,
    find_vmx_for_name as find_vmx_for_name,
)

