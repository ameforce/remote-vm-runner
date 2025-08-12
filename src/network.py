from __future__ import annotations

import ipaddress
from pathlib import Path
import logging
from typing import Callable

from .config import (
    EXCLUDE_SUBNETS,
    PREFERRED_SUBNETS,
    RDP_PORT,
    ASSUME_ACTIVE_ON_FAILURE,
    RDP_PS_TIMEOUT_SEC,
    RDP_QUSER_TIMEOUT_SEC,
)
from .guest import run_in_guest, run_in_guest_capture


logger = logging.getLogger(__name__)

def is_preferred_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if any(ip_obj in net for net in EXCLUDE_SUBNETS):
        return False
    return any(ip_obj in net for net in PREFERRED_SUBNETS)


def has_active_rdp_connections(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    ps_cmd = (
        f"$c=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue);"
        " if($c){'YES'} else {'NO'}"
    )
    out = None
    try:
        out = run_in_guest_capture(
            vmx,
            r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            "-NoProfile",
            "-Command",
            ps_cmd,
            timeout=RDP_PS_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.debug("RDP PS probe failed: %s", exc)
    if out:
        if "YES" in out:
            return True
        if "NO" in out:
            return False

    q_out = None
    try:
        q_out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=RDP_QUSER_TIMEOUT_SEC)
    except Exception as exc:
        logger.debug("RDP quser probe failed: %s", exc)
    if q_out:
        keywords = {" active ", "활성", "activo", "attivo", "aktív", "aktief"}
        for line in q_out.splitlines():
            low = line.strip().lower()
            if low.startswith("username"):
                continue
            if any(k in low for k in keywords):
                return True

    if ASSUME_ACTIVE_ON_FAILURE:
        logger.warning("RDP status inconclusive – assuming ACTIVE for safety: vmx=%s port=%s", vmx, rdp_port)
        return True
    return False


def renew_network(vmx: Path, on_progress: Callable[[str], None] | None = None) -> None:
    steps: list[tuple[str, list[str]]] = [
        ("IP 해제", [r"C:\\Windows\\System32\\ipconfig.exe", "/release"]),
        ("DHCP 갱신", [r"C:\\Windows\\System32\\ipconfig.exe", "/renew"]),
        ("DNS 플러시", [r"C:\\Windows\\System32\\ipconfig.exe", "/flushdns"]),
    ]

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    log("네트워크 재협상 시작")
    for title, cmd in steps:
        log(f"{title} 실행 중…")
        try:
            run_in_guest(vmx, cmd[0], *cmd[1:], timeout=60, retries=2, success_codes={0, 1})
            log(f"{title} 완료")
        except Exception as exc:
            log(f"{title} 실패: {exc}")
    log("네트워크 재협상 종료")
