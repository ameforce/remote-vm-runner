from __future__ import annotations

import re
from pathlib import Path
import logging

from .config import GUEST_PASS, GUEST_USER
from .vmrun import run_vmrun


logger = logging.getLogger(__name__)


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
                logger.debug(
                    "run_in_guest failed after %s attempts: program=%s vmx=%s error=%s",
                    attempt,
                    program,
                    vmx,
                    msg,
                )
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
    except Exception as exc:
        logger.debug(
            "run_in_guest_capture failed: program=%s vmx=%s error=%s",
            program,
            vmx,
            exc,
        )
        return ""


def run_script_in_guest_capture(
    vmx: Path,
    interpreter: str,
    script: str,
    *,
    timeout: int = 30,
) -> str:
    cmd_base: list[str] = [
        "-gu",
        GUEST_USER,
        "-gp",
        GUEST_PASS,
        "runScriptInGuest",
        str(vmx),
        interpreter,
        script,
    ]
    try:
        return run_vmrun(cmd_base, capture=True, timeout=timeout)
    except Exception as exc:
        logger.debug(
            "run_script_in_guest_capture failed: interpreter=%s vmx=%s error=%s",
            interpreter,
            vmx,
            exc,
        )
        return ""


def copy_from_guest(vmx: Path, guest_path: str, host_path: str, *, timeout: int = 30) -> bool:
    cmd_base: list[str] = [
        "-gu",
        GUEST_USER,
        "-gp",
        GUEST_PASS,
        "CopyFileFromGuestToHost",
        str(vmx),
        guest_path,
        host_path,
    ]
    try:
        run_vmrun(cmd_base, capture=True, timeout=timeout)
        return True
    except Exception as exc:
        logger.debug("copy_from_guest failed: src=%s dst=%s vmx=%s err=%s", guest_path, host_path, vmx, exc)
        return False
