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
    ASSUME_ACTIVE_IF_RDP_LISTENING,
    TCP_PROBE_TIMEOUT_SEC,
    RDP_CHECK_BUDGET_SEC,
    GUEST_USER,
    GUEST_PASS,
    ENABLE_TOOLS_SELF_HEAL,
    TOOLS_RESTART_COOLDOWN_SEC,
)
from .guest import run_in_guest, run_in_guest_capture, run_script_in_guest_capture, copy_from_guest
from .vmrun import run_vmrun
import socket
import tempfile
import time
import os


logger = logging.getLogger(__name__)

def is_preferred_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if any(ip_obj in net for net in EXCLUDE_SUBNETS):
        return False
    return any(ip_obj in net for net in PREFERRED_SUBNETS)


_LAST_TOOLS_RESTART: dict[str, float] = {}


def _maybe_restart_vmware_tools(vmx: Path) -> None:
    if not ENABLE_TOOLS_SELF_HEAL:
        return
    key = str(vmx)
    now = time.time()
    last = _LAST_TOOLS_RESTART.get(key, 0.0)
    if now - last < TOOLS_RESTART_COOLDOWN_SEC:
        return
    try:
        run_in_guest(
            vmx,
            r"C:\\Windows\\System32\\sc.exe",
            "stop",
            "VMTools",
            timeout=30,
        )
    except Exception as exc:
        logger.debug("VMTools stop failed: %s", exc)
    try:
        run_in_guest(
            vmx,
            r"C:\\Windows\\System32\\sc.exe",
            "start",
            "VMTools",
            timeout=30,
        )
        _LAST_TOOLS_RESTART[key] = now
        logger.warning("Attempted VMware Tools restart inside guest: vmx=%s", vmx)
    except Exception as exc:
        logger.debug("VMTools start failed: %s", exc)


def has_active_rdp_connections(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    start_ts = time.perf_counter()
    def over_budget() -> bool:
        return (time.perf_counter() - start_ts) > RDP_CHECK_BUDGET_SEC

    try:
        st = run_vmrun(["checkToolsState", str(vmx)], timeout=8)
        if "running" not in st.lower():
            if ASSUME_ACTIVE_ON_FAILURE:
                logger.warning("RDP check skipped – VMware Tools not running; assuming ACTIVE: vmx=%s", vmx)
                return True
            logger.warning("RDP check skipped – VMware Tools not running; assuming INACTIVE: vmx=%s", vmx)
            return False
    except Exception as exc:
        logger.debug("checkToolsState failed: %s", exc)

    if not over_budget():
        try:
            guest_tmp = r"C:\\Temp\\rdp_probe.txt"
            ps_script = (
                f"$c=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue); "
                "if($c){'YES'} else {'NO'} | Out-File -Encoding ASCII -Force '" + guest_tmp + "'"
            )
            run_script_in_guest_capture(
                vmx,
                r"C:\\Windows\\System32\\cmd.exe",
                f"/c powershell -NoProfile -Command \"{ps_script}\"",
                timeout=RDP_PS_TIMEOUT_SEC,
            )
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                host_tmp = tf.name
            try:
                if copy_from_guest(vmx, guest_tmp, host_tmp, timeout=10):
                    with open(host_tmp, "rb") as f:
                        data = f.read().decode("ascii", errors="ignore").strip()
                    if "YES" in data:
                        os.remove(host_tmp)
                        return True
                    if "NO" in data:
                        os.remove(host_tmp)
                        return False
            finally:
                try:
                    os.remove(host_tmp)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("file-based RDP probe failed: %s", exc)

    try:
        raw = run_vmrun(["-gu", GUEST_USER, "-gp", GUEST_PASS, "listProcessesInGuest", str(vmx)], timeout=10)
    except Exception as exc:
        logger.debug("listProcessesInGuest failed: %s", exc)
        raw = ""
    if raw:
        low = raw.lower()
        if "rdpclip.exe" in low:
            return True

    ps_script = (
        f"$c=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue); "
        "if($c){ Set-Content -Path 'C:\\Temp\\rdp_probe.txt' -Value 'YES' -Encoding ASCII } "
        "else{ Set-Content -Path 'C:\\Temp\\rdp_probe.txt' -Value 'NO' -Encoding ASCII }"
    )
    out = None
    ps_err: str | None = None
    try:
        out = run_script_in_guest_capture(
            vmx,
            r"C:\\Windows\\System32\\cmd.exe",
            f"/c powershell -NoProfile -Command \"{ps_script}\"; Get-Content 'C:\\Temp\\rdp_probe.txt'",
            timeout=RDP_PS_TIMEOUT_SEC,
        ).strip()
    except Exception as exc:
        ps_err = str(exc)
        logger.debug("RDP PS probe failed: %s", exc)
    if out:
        if "YES" in out:
            return True
        if "NO" in out:
            return False

    q_out = None
    quser_err: str | None = None
    try:
        q_out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=RDP_QUSER_TIMEOUT_SEC)
    except Exception as exc:
        quser_err = str(exc)
        logger.debug("RDP quser probe failed: %s", exc)
    if q_out:
        keywords = {" active ", "활성", "activo", "attivo", "aktív", "aktief"}
        for line in q_out.splitlines():
            low = line.strip().lower()
            if low.startswith("username"):
                continue
            if any(k in low for k in keywords):
                return True

    try:
        q2_out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\query.exe", "user", timeout=RDP_QUSER_TIMEOUT_SEC)
    except Exception as exc:
        logger.debug("RDP query user probe failed: %s", exc)
        q2_out = ""
    if q2_out:
        keywords = {" active ", "활성", "activo", "attivo", "aktív", "aktief"}
        for line in q2_out.splitlines():
            low = line.strip().lower()
            if any(k in low for k in keywords):
                return True

    try:
        qw_out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\qwinsta.exe", timeout=RDP_QUSER_TIMEOUT_SEC)
    except Exception as exc:
        logger.debug("RDP qwinsta probe failed: %s", exc)
        qw_out = ""
    if qw_out:
        if "active" in qw_out.lower() or "활성" in qw_out.lower():
            return True

    try:
        ns_out = run_in_guest_capture(
            vmx,
            r"C:\\Windows\\System32\\cmd.exe",
            "/c netstat -ano | find \"%d\" | find \"ESTABLISHED\"" % rdp_port,
            timeout=RDP_QUSER_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.debug("RDP netstat probe failed: %s", exc)
        ns_out = ""
    if ns_out and ns_out.strip():
        return True

    if ASSUME_ACTIVE_ON_FAILURE:
        if ASSUME_ACTIVE_IF_RDP_LISTENING:
            try:
                ip_raw = run_vmrun(["getGuestIPAddress", str(vmx)], timeout=10)
                ip = ip_raw.strip()
            except Exception:
                ip = ""
            if ip:
                try:
                    with socket.create_connection((ip, rdp_port), timeout=TCP_PROBE_TIMEOUT_SEC):
                        logger.warning(
                            "RDP status inconclusive – TCP probe succeeded; assuming ACTIVE: vmx=%s ip=%s port=%s",
                            vmx,
                            ip,
                            rdp_port,
                        )
                        return True
                except Exception:
                    pass
        _maybe_restart_vmware_tools(vmx)
        diag = ""
        try:
            who = run_script_in_guest_capture(
                vmx,
                r"C:\\Windows\\System32\\cmd.exe",
                "/c whoami",
                timeout=8,
            )
            diag = f"whoami={'none' if not who else who.strip()}"
        except Exception as exc:
            diag = f"whoami_error={exc}"
        logger.warning(
            "RDP status inconclusive – assuming ACTIVE for safety: vmx=%s port=%s ps_out=%s quser_out=%s ps_err=%s quser_err=%s %s",
            vmx,
            rdp_port,
            'none' if not out else 'len>0',
            'none' if not q_out else 'len>0',
            ps_err or 'none',
            quser_err or 'none',
            diag,
        )
        return True
    return False


def has_active_rdp_connections_fast(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    try:
        raw = run_vmrun(["-gu", GUEST_USER, "-gp", GUEST_PASS, "listProcessesInGuest", str(vmx)], timeout=5)
        if raw and "rdpclip.exe" in raw.lower():
            return True
    except Exception as exc:
        logger.debug("fast listProcessesInGuest failed: %s", exc)

    try:
        ip_raw = run_vmrun(["getGuestIPAddress", str(vmx)], timeout=5)
        ip = ip_raw.strip() if ip_raw else ""
        if ip:
            try:
                with socket.create_connection((ip, rdp_port), timeout=min(0.5, TCP_PROBE_TIMEOUT_SEC)):
                    return True
            except Exception:
                pass
    except Exception:
        pass

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
