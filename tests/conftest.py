from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
