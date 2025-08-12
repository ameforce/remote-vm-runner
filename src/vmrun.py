from __future__ import annotations

import subprocess
from typing import Iterable

from .config import VMRUN


def run_vmrun(args: Iterable[str], capture: bool = True, timeout: int = 120) -> str:
    cmd = [str(VMRUN), "-T", "ws", *args]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=True,
            encoding="utf-8",
            timeout=timeout,
        )
        return completed.stdout.strip()
    except subprocess.TimeoutExpired as exc:
        try:
            if exc.process:
                exc.process.kill()
        except Exception:
            pass
        return ""
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"vmrun failed: {exc.stderr.strip()}") from exc
