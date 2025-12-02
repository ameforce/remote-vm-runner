from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import time
from pathlib import Path

from .config import (
    ENABLE_TOOLS_SELF_HEAL,
    RDP_PORT,
    RDP_PS_TIMEOUT_SEC,
    RDP_QUSER_TIMEOUT_SEC,
    TOOLS_RESTART_COOLDOWN_SEC,
)
from .guest import run_in_guest, run_in_guest_capture
from .vmrun import run_vmrun


logger = logging.getLogger(__name__)

_ACTIVE_KEYWORDS = {" active ", "활성", "activo", "attivo", "aktív", "aktief", "active"}
_LAST_TOOLS_RESTART: dict[str, float] = {}
_TOOLS_EVER_RUNNING: dict[str, bool] = {}


def _is_vm_running(vmx: Path) -> bool:
    try:
        status = run_vmrun(["list"], timeout=6)
        return str(vmx) in status
    except Exception:
        return False


def _line_has_active_keyword(text: str) -> bool:
    low = f" {text.lower()} "
    return any(k in low for k in _ACTIVE_KEYWORDS)


def _line_is_remote_session(text: str) -> bool:
    low = text.lower()
    if "console" in low:
        return False
    return bool("rdp-tcp" in low or re.search(r"\brdp[-]tcp#?\d*\b", low))


def _line_is_active_remote_session(text: str) -> bool:
    return _line_is_remote_session(text) and _line_has_active_keyword(text)


def _check_tools_state(vmx: Path) -> str:
    try:
        state = run_vmrun(["checkToolsState", str(vmx)], timeout=6)
        return (state or "").strip()
    except Exception as exc:
        logger.debug("checkToolsState failed: %s", exc)
        return ""


def _maybe_restart_vmware_tools(vmx: Path) -> bool:
    if not _is_vm_running(vmx):
        return False
    if not ENABLE_TOOLS_SELF_HEAL:
        return False
    key = str(vmx)
    # 부팅 직후 Tools가 아직 한 번도 정상 동작한 적이 없는 상태에서는
    # self-heal을 시도하면 VMTools가 바로 죽어버리는 현상이 있으므로 절대 금지.
    if not _TOOLS_EVER_RUNNING.get(key, False):
        return False
    now = time.time()
    last = _LAST_TOOLS_RESTART.get(key, 0.0)
    if now - last < TOOLS_RESTART_COOLDOWN_SEC:
        return False
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
        return True
    except Exception as exc:
        logger.debug("VMTools start failed: %s", exc)
        return False


def probe_rdp_usage(vmx: Path, rdp_port: int = RDP_PORT) -> tuple[bool, list[str], str]:
    tools_state_raw = _check_tools_state(vmx)
    tools_running = True
    if tools_state_raw:
        state_norm = tools_state_raw.strip().lower()
        tools_running = state_norm == "running"
        if tools_running:
            try:
                _TOOLS_EVER_RUNNING[str(vmx)] = True
            except Exception:
                pass

    active = False
    clients: list[str] = []

    usernames_reliable = False
    try:
        users, usernames_reliable = _get_active_rdp_usernames_best_with_status(vmx)
    except Exception as exc:
        logger.debug("RDP username probe failed: %s", exc)
        users = []
        usernames_reliable = False
    if users:
        active = True
        clients = users
    elif not usernames_reliable:
        try:
            ips = get_active_rdp_remote_ips(vmx, rdp_port=rdp_port)
        except Exception as exc:
            logger.debug("RDP TCP probe failed: %s", exc)
            ips = []
        if ips:
            active = True

    if active:
        logger.debug(
            "probe_rdp_usage: vmx=%s tools_state=%r tools_running=%s ips>0 users=%s -> active",
            vmx,
            tools_state_raw,
            tools_running,
            clients,
        )
        return True, clients, "active"

    if not tools_running:
        restarted = _maybe_restart_vmware_tools(vmx)
        status = "tools_restarting" if restarted else "tools_error"
        logger.debug(
            "probe_rdp_usage: vmx=%s tools_state=%r tools_running=%s -> status=%s",
            vmx,
            tools_state_raw,
            tools_running,
            status,
        )
        return False, [], status

    logger.debug(
        "probe_rdp_usage: vmx=%s tools_state=%r tools_running=%s ips=[] users=[] -> none",
        vmx,
        tools_state_raw,
        tools_running,
    )
    return False, [], "none"


def has_active_rdp_connections_tcp(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    active, _clients, _status = probe_rdp_usage(vmx, rdp_port=rdp_port)
    return bool(active)


def get_active_rdp_remote_ips(vmx: Path, rdp_port: int = RDP_PORT) -> list[str]:
    if not _is_vm_running(vmx):
        return []
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
            uniq = list(dict.fromkeys(remotes))
            logger.debug("get_active_rdp_remote_ips: vmx=%s ips=%s", vmx, uniq)
            return uniq

    logger.debug("get_active_rdp_remote_ips: vmx=%s ips=[]", vmx)
    return []


def _get_active_rdp_usernames_guest_with_status(vmx: Path) -> tuple[list[str], bool]:
    if not _is_vm_running(vmx):
        return [], False
    outputs: list[str] = []
    any_success = False
    try:
        out = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\query.exe", "user", timeout=RDP_QUSER_TIMEOUT_SEC)
        if out:
            any_success = True
            outputs.append(out)
    except Exception as exc:
        logger.debug("query user for usernames failed: %s", exc)
    try:
        out2 = run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=RDP_QUSER_TIMEOUT_SEC)
        if out2:
            any_success = True
            outputs.append(out2)
    except Exception as exc:
        logger.debug("quser for usernames failed: %s", exc)
    usernames: list[str] = []
    for out in outputs:
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.lower().startswith("username"):
                continue
            if not _line_is_active_remote_session(line):
                continue
            token_line = line.lstrip(">").strip()
            first = token_line.split()
            if first:
                name = first[0]
                if name and name.lower() != "username" and name not in usernames:
                    usernames.append(name)
    logger.debug("get_active_rdp_usernames (guest): vmx=%s users=%s", vmx, usernames)
    return usernames, any_success


def get_active_rdp_usernames(vmx: Path) -> list[str]:
    users, _ok = _get_active_rdp_usernames_guest_with_status(vmx)
    return users


def _get_guest_ip(vmx: Path) -> str:
    try:
        ip_raw = run_vmrun(["getGuestIPAddress", str(vmx), "-wait"], timeout=4)
        return (ip_raw or "").strip()
    except Exception as exc:
        logger.debug("_get_guest_ip failed: %s", exc)
        return ""


def _get_active_rdp_usernames_host_with_status(ip: str) -> tuple[list[str], bool]:
    if not ip:
        return [], False
    commands = [
        [r"C:\\Windows\\System32\\query.exe", "user", f"/server:{ip}"],
        [r"C:\\Windows\\System32\\quser.exe", f"/server:{ip}"],
        [r"C:\\Windows\\System32\\qwinsta.exe", f"/server:{ip}"],
    ]
    outputs: list[str] = []
    any_success = False
    for cmd in commands:
        try:
            try:
                if str(cmd[0]).lower().endswith("query.exe"):
                    logger.debug("host query.exe cmd: %s", " ".join(cmd))
            except Exception:
                pass
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=min(4, RDP_QUSER_TIMEOUT_SEC),
            )
            out = (cp.stdout or "").strip()
            if cp.returncode == 0:
                any_success = True
            if out:
                outputs.append(out)
        except Exception as exc:
            logger.debug("host-side session probe failed: %s cmd=%s", exc, cmd)
    users: list[str] = []
    for out in outputs:
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.lower().startswith("username"):
                continue
            if not _line_is_active_remote_session(line):
                continue
            token_line = line.lstrip(">").strip()
            parts = token_line.split()
            if parts:
                name = parts[0]
                if name and name.lower() != "username" and name not in users:
                    users.append(name)
    logger.debug("get_active_rdp_usernames_host: ip=%s users=%s", ip, users)
    return users, any_success


def get_active_rdp_usernames_host(ip: str) -> list[str]:
    users, _ok = _get_active_rdp_usernames_host_with_status(ip)
    return users


def _get_active_rdp_usernames_best_with_status(vmx: Path) -> tuple[list[str], bool]:
    ip = _get_guest_ip(vmx)
    users, host_ok = _get_active_rdp_usernames_host_with_status(ip)
    if host_ok:
        return users, True
    users, guest_ok = _get_active_rdp_usernames_guest_with_status(vmx)
    return users, guest_ok


def get_active_rdp_usernames_best(vmx: Path) -> list[str]:
    users, _ok = _get_active_rdp_usernames_best_with_status(vmx)
    return users
