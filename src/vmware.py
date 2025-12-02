from __future__ import annotations

import re
import subprocess
import time
import threading
from pathlib import Path
from typing import Callable
import socket
import psutil
import logging
import shutil

from .config import (
    IP_POLL_INTERVAL,
    IP_POLL_TIMEOUT,
    VMRUN,
    DHCP_LEASES_PATHS_RAW,
    RDP_PORT,
    RDP_READY_WAIT_SEC,
    RDP_READY_PROBE_INTERVAL_SEC,
)
from .guest import run_in_guest_capture
from .network import is_preferred_ip
from .vmrun import run_vmrun


logger = logging.getLogger(__name__)

def ensure_vm_running(
    vmx: Path,
    timeout: int = 60,
    probe_interval: float = 0.5,
    on_progress: Callable[[str], None] | None = None,
) -> None:
    if is_vm_running(vmx):
        return
    if on_progress:
        try:
            on_progress("전원 켜는 중")
        except Exception:
            pass
    try:
        logger.info("VM is powered off; starting: vmx=%s", vmx)
    except Exception:
        pass
    start_vm_async(vmx)
    start_ts = time.perf_counter()
    fallback_done = False
    while True:
        if is_vm_running(vmx):
            try:
                logger.info("VM power on confirmed: vmx=%s elapsed=%.2fs", vmx, time.perf_counter() - start_ts)
            except Exception:
                pass
            return
        elapsed = time.perf_counter() - start_ts
        if not fallback_done and elapsed > min(timeout * 0.33, 10):
            try:
                if on_progress:
                    on_progress("전원 켜는 중(폴백)")
            except Exception:
                pass
            try:
                run_vmrun(["start", str(vmx), "nogui"], timeout=60)
            except Exception as exc:
                try:
                    logger.warning("Synchronous start fallback failed: vmx=%s err=%s", vmx, exc)
                except Exception:
                    pass
                msg = (str(exc) or "").lower()
                if "already in use" in msg and not is_vm_running(vmx) and not _has_vm_process(vmx):
                    try:
                        _try_clear_stale_locks(vmx)
                    except Exception:
                        pass
                    try:
                        run_vmrun(["start", str(vmx), "nogui"], timeout=60)
                    except Exception as exc2:
                        try:
                            logger.error("Start after lock cleanup failed: vmx=%s err=%s", vmx, exc2)
                        except Exception:
                            pass
            fallback_done = True
        if elapsed > timeout:
            raise TimeoutError("VM 전원을 켤 수 없습니다(타임아웃)")
        time.sleep(probe_interval)


def is_vm_running(vmx: Path) -> bool:
    try:
        status = run_vmrun(["list"], timeout=10)
        return str(vmx) in status
    except Exception:
        return False


def list_snapshots(vmx: Path) -> list[str]:
    raw = run_vmrun(["listSnapshots", str(vmx)])
    return [ln.strip() for ln in raw.splitlines()[1:]]


def start_vm_async(vmx: Path) -> None:
    def run_start_command() -> None:
        try:
            subprocess.Popen(
                [str(VMRUN), "-T", "ws", "start", str(vmx), "nogui"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            try:
                logger.warning("Popen start failed, falling back to synchronous start: vmx=%s err=%s", vmx, exc)
            except Exception:
                pass
            try:
                run_vmrun(["start", str(vmx), "nogui"], timeout=60)
            except Exception as exc2:
                try:
                    logger.error("Fallback start failed: vmx=%s err=%s", vmx, exc2)
                except Exception:
                    pass
    threading.Thread(target=run_start_command, daemon=True).start()


def tools_ready(vmx: Path) -> bool:
    try:
        state = run_vmrun(["checkToolsState", str(vmx)], timeout=10)
        return "running" in state.lower()
    except Exception:
        return False


def wait_for_tools_ready(
    vmx: Path, timeout: int = 60, probe_interval: float = 0.1, on_progress: Callable[[str], None] | None = None
) -> None:
    start = time.perf_counter()
    while True:
        if tools_ready(vmx):
            if on_progress:
                on_progress("VMware Tools 준비 완료")
            return
        if time.perf_counter() - start > timeout:
            raise TimeoutError("VMware Tools 준비 타임아웃")
        time.sleep(probe_interval)


def fast_wait_for_ip(
    vmx: Path,
    timeout: int = 60,
    probe_interval: float = 0.2,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    start_time = time.perf_counter()
    last_ip = ""
    headless_last = 0.0
    guest_last = 0.0
    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError("fast_wait_for_ip: 시간 초과")
        try:
            raw = run_vmrun(["getGuestIPAddress", str(vmx), "-wait"], capture=True, timeout=10)
            ip = raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if on_progress and ip != last_ip:
                    on_progress(f"IP 확인: {ip}")
                    last_ip = ip
                return ip
        except Exception:
            pass
        now = time.perf_counter()
        if now - headless_last >= 1.0 or not last_ip:
            headless_last = now
            ip2 = _headless_lookup_ip(vmx)
            if ip2:
                if on_progress and ip2 != last_ip:
                    on_progress(f"헤드리스: DHCP/ARP에서 IP 확인: {ip2}")
                    last_ip = ip2
                return ip2
        if now - guest_last >= 1.0:
            guest_last = now
            ip3 = _guest_query_ipv4(vmx)
            if ip3:
                if on_progress and ip3 != last_ip:
                    on_progress(f"게스트 내부에서 IP 확인: {ip3}")
                    last_ip = ip3
                return ip3
        time.sleep(probe_interval)


def wait_for_vm_ready(
    vmx: Path,
    timeout: int | None = None,
    probe_interval: float | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    if timeout is None:
        timeout = IP_POLL_TIMEOUT
    if probe_interval is None:
        probe_interval = IP_POLL_INTERVAL

    def _ping_ok(host: str) -> bool:
        try:
            res = subprocess.run(["ping", "-n", "1", "-w", "600", host], capture_output=True, text=True, timeout=2)
            return "TTL=" in res.stdout
        except Exception:
            return False

    start_time = time.perf_counter()
    last_ip = ""
    headless_last = 0.0
    guest_last = 0.0
    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError(f"{timeout}초 내에 유효한 IP를 확인하지 못했습니다")
        try:
            ip_raw = run_vmrun(["getGuestIPAddress", str(vmx), "-wait"], capture=True, timeout=10)
            ip = ip_raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if on_progress and ip != last_ip:
                    on_progress(f"IP 취득 중: {ip}")
                    last_ip = ip
                if on_progress:
                    on_progress("IP 검증 완료!")
                return ip
        except Exception:
            pass
        now = time.perf_counter()
        if now - headless_last >= 1.0 or not last_ip:
            headless_last = now
            ip2 = _headless_lookup_ip(vmx)
            if ip2:
                if on_progress and ip2 != last_ip:
                    on_progress(f"헤드리스: DHCP/ARP에서 IP 확인: {ip2}")
                    last_ip = ip2
                return ip2
        if now - guest_last >= 1.0:
            guest_last = now
            ip3 = _guest_query_ipv4(vmx)
            if ip3:
                if on_progress and ip3 != last_ip:
                    on_progress(f"게스트 내부에서 IP 확인: {ip3}")
                    last_ip = ip3
                return ip3
        time.sleep(probe_interval)


def wait_for_rdp_ready(
    vmx: Path,
    ip: str,
    port: int | None = None,
    timeout: float | None = None,
    probe_interval: float | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> bool:
    p = port or RDP_PORT
    tout = timeout if isinstance(timeout, (int, float)) and timeout else RDP_READY_WAIT_SEC
    interval = probe_interval if isinstance(probe_interval, (int, float)) and probe_interval else RDP_READY_PROBE_INTERVAL_SEC
    start = time.perf_counter()
    while True:
        try:
            try:
                if not is_vm_running(vmx):
                    if on_progress:
                        on_progress("VM 전원 꺼짐 감지 – 전원 켜는 중")
                    start_vm_async(vmx)
                    time.sleep(min(2.0, interval))
            except Exception:
                pass
            with socket.create_connection((ip, p), timeout=1.0):
                if on_progress:
                    on_progress("RDP 포트 준비 완료")
                return True
        except Exception:
            pass
        if time.perf_counter() - start > tout:
            return False
        if on_progress:
            on_progress("RDP 대기 중…")
        time.sleep(interval)


def _normalize_mac_colon(mac: str) -> str:
    m = mac.strip().lower().replace("-", ":")
    if len(m) == 17:
        return m
    return m


def _vmx_primary_mac(vmx: Path) -> str:
    try:
        text = vmx.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    address = None
    generated = None
    present_nics: set[str] = set()
    for line in text.splitlines():
        ln = line.strip()
        m_present = re.match(r"ethernet(\d+)\.present\s*=\s*\"(true|TRUE)\"", ln)
        if m_present:
            present_nics.add(m_present.group(1))
        m_addr = re.match(r"ethernet(\d+)\.address\s*=\s*\"([0-9A-Fa-f:\-]{17})\"", ln)
        if m_addr and (m_addr.group(1) in present_nics or address is None):
            address = _normalize_mac_colon(m_addr.group(2))
        m_gen = re.match(r"ethernet(\d+)\.generatedAddress\s*=\s*\"([0-9A-Fa-f:\-]{17})\"", ln)
        if m_gen and (m_gen.group(1) in present_nics or generated is None):
            generated = _normalize_mac_colon(m_gen.group(2))
    return address or generated or ""


def _dhcp_candidate_paths() -> list[Path]:
    paths: list[Path] = []
    if DHCP_LEASES_PATHS_RAW:
        for p in DHCP_LEASES_PATHS_RAW.replace(",", ";").split(";"):
            p = p.strip()
            if p:
                paths.append(Path(p))
    defaults = [
        r"C:\\ProgramData\\VMware\\vmnetdhcp.leases",
        r"C:\\ProgramData\\VMware\\vmnetdhcp\\vmnetdhcp.leases",
        r"C:\\ProgramData\\VMware\\vmnetdhcp\\vmnetdhcp-Vmnet8.leases",
        r"C:\\ProgramData\\VMware\\vmnetdhcp\\vmnetdhcp-Vmnet1.leases",
    ]
    for d in defaults:
        paths.append(Path(d))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def _parse_dhcp_leases_for_mac(paths: list[Path], mac: str) -> str:
    target = _normalize_mac_colon(mac)
    last_ip = ""
    for p in paths:
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        current_ip = None
        for line in data.splitlines():
            ln = line.strip()
            m_lease = re.match(r"lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{", ln)
            if m_lease:
                current_ip = m_lease.group(1)
                continue
            if current_ip:
                if re.search(rf"hardware\s+ethernet\s+{re.escape(target)}", ln, flags=re.IGNORECASE):
                    last_ip = current_ip
                if ln.startswith("}"):
                    current_ip = None
    return last_ip


def _arp_lookup_ip(mac: str) -> str:
    norm_dash = _normalize_mac_colon(mac).replace(":", "-")
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=3)
        txt = out.stdout or ""
    except Exception:
        return ""
    for line in txt.splitlines():
        ln = line.strip().lower()
        m = re.match(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f\-]{17})\s+(dynamic|static)", ln)
        if m and m.group(2) == norm_dash:
            return m.group(1)
    return ""


def _headless_lookup_ip(vmx: Path) -> str:
    mac = _vmx_primary_mac(vmx)
    if not mac:
        return ""
    ip = _parse_dhcp_leases_for_mac(_dhcp_candidate_paths(), mac)
    if ip and is_preferred_ip(ip):
        return ip
    if not ip:
        ip = _arp_lookup_ip(mac)
    if ip and is_preferred_ip(ip):
        return ip
    return ip or ""


def _guest_query_ipv4(vmx: Path) -> str:
    ps = (
        "$ips=(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
        "Where-Object { $_.IPAddress -and $_.IPAddress -notmatch '^169\\.254\\.' } | "
        "Select-Object -ExpandProperty IPAddress | Sort-Object -Unique); "
        "if($ips){ $ips -join '\\n' }"
    )
    out = run_in_guest_capture(
        vmx,
        r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
        "-NoProfile",
        "-Command",
        ps,
        timeout=15,
    )
    if not out:
        return ""
    for raw in out.splitlines():
        ip = raw.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip) and is_preferred_ip(ip):
            return ip
    for raw in out.splitlines():
        ip = raw.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            return ip
    return ""
def _has_vm_process(vmx: Path) -> bool:
    vmx_str = str(vmx).lower()
    stem = vmx.stem.lower()
    try:
        for proc in psutil.process_iter(["name", "cmdline"]):
            name = (proc.info.get("name") or "").lower()
            if not name.startswith("vmware-vmx"):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if vmx_str in cmdline or stem in cmdline:
                return True
    except Exception:
        return False
    return False


def _try_clear_stale_locks(vmx: Path) -> bool:
    vm_dir = vmx.parent
    removed_any = False
    try:
        for p in vm_dir.glob("*.lck"):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                removed_any = True
            except Exception:
                pass
    except Exception:
        return removed_any
    return removed_any
