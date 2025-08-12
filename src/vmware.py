from __future__ import annotations

import re
import subprocess
import time
import threading
from pathlib import Path
from typing import Callable, Iterable

from .config import GUEST_PASS, GUEST_USER, IP_POLL_INTERVAL, IP_POLL_TIMEOUT, VMRUN
from .network import renew_network
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
            if m:
                exit_code = int(m.group(1))
                if exit_code in success_codes:
                    return
            if attempt == retries:
                return
            time.sleep(2)


def run_in_guest_capture(
    vmx: Path,
    program: str,
    *args: str,
    timeout: int = 30,
) -> str:
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
    try:
        return run_vmrun(cmd_base, capture=True, timeout=timeout)
    except Exception:
        return ""


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
    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError("fast_wait_for_ip: 시간 초과")
        try:
            raw = run_vmrun(["getGuestIPAddress", str(vmx)], capture=True, timeout=10)
            ip = raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if on_progress and ip != last_ip:
                    on_progress(f"IP 확인: {ip}")
                    last_ip = ip
                return ip
        except Exception:
            pass
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
    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError(f"{timeout}초 내에 유효한 IP를 확인하지 못했습니다")
        try:
            ip_raw = run_vmrun(["getGuestIPAddress", str(vmx)], capture=True, timeout=10)
            ip = ip_raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if on_progress and ip != last_ip:
                    on_progress(f"IP 취득 중: {ip}")
                    last_ip = ip
                if not _ping_ok(ip):
                    if on_progress:
                        on_progress("핑 응답 없음 – 네트워크 재협상")
                    renew_network(vmx, on_progress=on_progress)
                    continue
                if on_progress:
                    on_progress("IP 검증 완료!")
                return ip
        except Exception:
            pass
        time.sleep(probe_interval)
