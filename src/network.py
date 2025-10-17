from __future__ import annotations

import ipaddress
from pathlib import Path
import logging
from typing import Callable
import re

from .config import (
    EXCLUDE_SUBNETS,
    PREFERRED_SUBNETS,
    RDP_PORT,
    RDP_PS_TIMEOUT_SEC,
    RDP_QUSER_TIMEOUT_SEC,
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
import subprocess


logger = logging.getLogger(__name__)


_ACTIVE_KEYWORDS = {" active ", "활성", "activo", "attivo", "aktív", "aktief", "active"}


def _line_has_active_keyword(text: str) -> bool:
    low = f" {text.lower()} "
    return any(k in low for k in _ACTIVE_KEYWORDS)


def _line_is_remote_session(text: str) -> bool:
    low = text.lower()
    if "console" in low:
        return False
    if "rdp-tcp" in low or "rdp" in low:
        return True
    return bool(re.search(r"\brdp[-]tcp#?\d*\b", low))


def _has_remote_active_from_session_tools(output: str) -> bool:
    if not output:
        return False
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower().startswith("username"):
            continue
        if _line_is_remote_session(line) and _line_has_active_keyword(line):
            return True
    return False

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


def has_active_rdp_connections_tcp(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    try:
        ip_raw = run_vmrun(["getGuestIPAddress", str(vmx)], timeout=6)
        ip = ip_raw.strip()
        if not ip:
            return False
    except Exception:
        return False
    try:
        with socket.create_connection((ip, rdp_port), timeout=TCP_PROBE_TIMEOUT_SEC):
            return True
    except Exception:
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


def get_active_rdp_remote_ips(vmx: Path, rdp_port: int = RDP_PORT) -> list[str]:
    try:
        ps_cmd = (
            f"$ips=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue | "
            "Select-Object -ExpandProperty RemoteAddress | Sort-Object -Unique); "
            "if($ips){ $ips -join '\n' }"
        )
        out = run_in_guest_capture(
            vmx,
            r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
            "-NoProfile",
            "-Command",
            ps_cmd,
            timeout=RDP_PS_TIMEOUT_SEC,
        )
        if out:
            ips = [ln.strip() for ln in out.splitlines() if ln.strip()]
            cleaned: list[str] = []
            for ip in ips:
                try:
                    ip_clean = ip.split("%", 1)[0]
                    ipaddress.ip_address(ip_clean)
                    cleaned.append(ip_clean)
                except Exception:
                    pass
            if cleaned:
                return list(dict.fromkeys(cleaned))
    except Exception as exc:
        logger.debug("PS get_active_rdp_remote_ips failed: %s", exc)

    try:
        ns_out = run_in_guest_capture(
            vmx,
            r"C:\\Windows\\System32\\cmd.exe",
            "/c",
            f"netstat -ano | find \"ESTABLISHED\" | find \":{rdp_port}\"",
            timeout=RDP_QUSER_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.debug("netstat get_active_rdp_remote_ips failed: %s", exc)
        ns_out = ""

    if ns_out:
        remotes: list[str] = []
        for line in ns_out.splitlines():
            parts = [p for p in line.split() if p]
            if len(parts) >= 3:
                remote = parts[2]
                try:
                    if remote.startswith("[") and "]" in remote:
                        host = remote[1:].split("]", 1)[0]
                    else:
                        host = remote.rsplit(":", 1)[0]
                    host = host.split("%", 1)[0]
                    ipaddress.ip_address(host)
                    remotes.append(host)
                except Exception:
                    continue
        if remotes:
            return list(dict.fromkeys(remotes))

    return []


def get_active_rdp_usernames(vmx: Path) -> list[str]:
    outputs: list[str] = []
    try:
        out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\query.exe", "user", timeout=RDP_QUSER_TIMEOUT_SEC)
        if out:
            outputs.append(out)
    except Exception as exc:
        logger.debug("query user for usernames failed: %s", exc)
    try:
        out2 = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=RDP_QUSER_TIMEOUT_SEC)
        if out2:
            outputs.append(out2)
    except Exception as exc:
        logger.debug("quser for usernames failed: %s", exc)
    usernames: list[str] = []
    for out in outputs:
        for raw in out.splitlines():
            line = raw.strip()
            if not line or line.lower().startswith("username"):
                continue
            if not (_line_is_remote_session(line) and _line_has_active_keyword(line)):
                continue
            token_line = line.lstrip(">").strip()
            first = token_line.split()
            if first:
                name = first[0]
                if name and name.lower() != "username" and name not in usernames:
                    usernames.append(name)
    return usernames


def _get_guest_ip_quick(vmx: Path) -> str:
    try:
        ip_raw = run_vmrun(["getGuestIPAddress", str(vmx)], timeout=3)
        return (ip_raw or "").strip()
    except Exception:
        return ""


def get_active_rdp_usernames_host(ip: str) -> list[str]:
    if not ip:
        return []
    commands = [
        [r"C:\\Windows\\System32\\query.exe", "user", f"/server:{ip}"],
        [r"C:\\Windows\\System32\\quser.exe", f"/server:{ip}"],
        [r"C:\\Windows\\System32\\qwinsta.exe", f"/server:{ip}"],
    ]
    outputs: list[str] = []
    for cmd in commands:
        try:
            try:
                if str(cmd[0]).lower().endswith("query.exe"):
                    logger.debug("host query.exe cmd: %s", " ".join(cmd))
            except Exception:
                pass
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            out = (cp.stdout or "").strip()
            if out:
                outputs.append(out)
        except Exception as exc:
            logger.debug("host-side session probe failed: %s cmd=%s", exc, cmd)
    users: list[str] = []
    for out in outputs:
        for raw in out.splitlines():
            line = raw.strip()
            if not line or line.lower().startswith("username"):
                continue
            if not (_line_is_remote_session(line) and _line_has_active_keyword(line)):
                continue
            token_line = line.lstrip(">").strip()
            parts = token_line.split()
            if parts:
                name = parts[0]
                if name and name.lower() != "username" and name not in users:
                    users.append(name)
    return users


def get_active_rdp_usernames_best(vmx: Path) -> list[str]:
    users = get_active_rdp_usernames(vmx)
    if users:
        return users
    ip = _get_guest_ip_quick(vmx)
    return get_active_rdp_usernames_host(ip)
