from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .config import GUEST_PASS, GUEST_USER
from .vmrun import run_vmrun


def run_in_guest(
    vmx: Path,
    program: str,
    *args: str,
    timeout: int = 60,
    retries: int = 3,
    success_codes: set[int] | None = None,
) -> None:
    cmd_base = [
        "-gu",
        GUEST_USER,
        "-gp",
        GUEST_PASS,
        "runProgramInGuest",
        str(vmx),
        program,
        *args,
    ]
    if success_codes is None:
        success_codes = {0}
    for attempt in range(1, retries + 1):
        try:
            run_vmrun(cmd_base, capture=True, timeout=timeout)
            return
        except RuntimeError as exc:
            msg = str(exc)
            m = re.search(r"exit code:\s*(\d+)", msg)
            if m and int(m.group(1)) in success_codes:
                return
            if attempt == retries:
                return


def run_in_guest_capture(
    vmx: Path,
    program: str,
    *args: str,
    timeout: int = 30,
) -> str:
    cmd_base: list[str] = [
        "-gu",
        GUEST_USER,
        "-gp",
        GUEST_PASS,
        "runProgramInGuest",
        str(vmx),
        program,
        *args,
    ]
    try:
        return run_vmrun(cmd_base, capture=True, timeout=timeout)
    except Exception:
        return ""
