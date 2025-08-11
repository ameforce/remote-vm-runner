from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import List
from uuid import uuid4

import logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("qa-vm-api")

import builtins

def _info_print(*args, **kwargs):
    logger.info(' '.join(str(a) for a in args))

builtins.print = _info_print

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
import ipaddress
import socket
import sys
import threading
from contextlib import asynccontextmanager

if os.name == "nt":
    import winreg

    _REG_KEY = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\\QA_VM_API\\Durations")

    def _load_samples(name: str) -> list[float]:
        try:
            data, _ = winreg.QueryValueEx(_REG_KEY, name)
            return [float(x) for x in data.split(',') if x]
        except FileNotFoundError:
            return []
        except OSError:
            return []

    def record_duration(name: str, secs: float, limit: int = 10) -> None:
        samples = _load_samples(name)
        samples.append(secs)
        samples = samples[-limit:]
        winreg.SetValueEx(_REG_KEY, name, 0, winreg.REG_SZ, ','.join(f"{s:.1f}" for s in samples))

    def average_duration(name: str) -> float | None:
        samples = _load_samples(name)
        if not samples:
            return None
        return sum(samples) / len(samples)
else:
    def record_duration(name: str, secs: float, limit: int = 10) -> None:
        pass

    def average_duration(name: str) -> float | None:
        return None


def _calc_poll_params(vm: str, op: str) -> tuple[float, int]:
    return IP_POLL_INTERVAL, IP_POLL_TIMEOUT


VMRUN = Path(os.getenv("VMRUN_PATH", r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"))
VM_MAP = {
    "init": Path(r"C:\VMware\Windows Server 2025\Windows Server 2025.vmx"),
    "hwp2018": Path(r"C:\VMware\Windows Server 2018 - HWP 2018\HWP 2018.vmx"),
    "hwp2022": Path(r"C:\VMware\Windows Server 2022 - HWP 2022\HWP 2022.vmx"),
    "hwp2024": Path(r"C:\VMware\Windows Server 2025 - HWP 2024\HWP 2024.vmx"),
}
VM_ROOT = Path(os.getenv("VM_ROOT", r"C:\VMware"))
IP_POLL_INTERVAL = 0.2
IP_POLL_TIMEOUT = 120
_pref_env = os.getenv("PREFERRED_SUBNETS", "192.168.0.0/22")
PREFERRED_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _pref_env.split(',') if net.strip()]
_ex_env = os.getenv("EXCLUDE_SUBNETS", "")
EXCLUDE_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _ex_env.split(',') if net.strip()]

GUEST_USER = os.getenv("GUEST_USER", "administrator")
GUEST_PASS = os.getenv("GUEST_PASS", "epapyrus12#$")

ENABLE_IDLE_WATCHDOG = True
IDLE_CHECK_INTERVAL_SEC = int(os.getenv("IDLE_CHECK_INTERVAL_SEC", "60"))
IDLE_SHUTDOWN_MINUTES = int(os.getenv("IDLE_SHUTDOWN_MINUTES", "30"))
IDLE_SHUTDOWN_SECONDS = max(30, IDLE_SHUTDOWN_MINUTES * 60)
IDLE_SHUTDOWN_MODE = os.getenv("IDLE_SHUTDOWN_MODE", "soft")
RDP_PORT = int(os.getenv("RDP_PORT", "3389"))

# Resource pressure thresholds
MIN_AVAILABLE_MEM_GB = float(os.getenv("MIN_AVAILABLE_MEM_GB", "6"))
MAX_SHUTDOWNS_PER_TICK = int(os.getenv("MAX_SHUTDOWNS_PER_TICK", "2"))
CPU_PRESSURE_THRESHOLD_PCT = int(os.getenv("CPU_PRESSURE_THRESHOLD_PCT", "95"))
CPU_SAMPLE_DURATION_SEC = float(os.getenv("CPU_SAMPLE_DURATION_SEC", "0.2"))
CPU_CONSECUTIVE_TICKS = int(os.getenv("CPU_CONSECUTIVE_TICKS", "3"))

# runtime counters
_CPU_OVER_LIMIT_COUNT = 0


def _run_in_guest(
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
            _run_vmrun(cmd_base, capture=True, timeout=timeout)
            return
        except RuntimeError as e:
            msg = str(e)
            m = re.search(r"exit code:\s*(\d+)", msg)
            if m:
                exit_code = int(m.group(1))
                if exit_code in success_codes:
                    logger.info("runProgramInGuest 허용 종료 코드(%d): %s", exit_code, msg)
                    return
            logger.warning("runProgramInGuest 실패(%d/%d): %s", attempt, retries, e)
            if attempt == retries:
                return
            try:
                wait_for_tools_ready(vmx, timeout=10, probe_interval=0.5)
            except Exception:
                pass
            time.sleep(2)


def _run_in_guest_capture(
    vmx: Path,
    program: str,
    *args: str,
    timeout: int = 30,
) -> str:
    """Run a program in guest and capture stdout.

    Best-effort: returns empty string on failure.
    """
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
        return _run_vmrun(cmd_base, capture=True, timeout=timeout)
    except Exception as e:
        logger.debug("run_in_guest_capture 실패: %s", e)
        return ""


def has_active_rdp_connections(vmx: Path, rdp_port: int = RDP_PORT) -> bool:
    ps_cmd = (
        f"$c=(Get-NetTCPConnection -LocalPort {rdp_port} -State Established -ErrorAction SilentlyContinue);"
        " if($c){'YES'} else {'NO'}"
    )
    out = _run_in_guest_capture(vmx, r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-Command", ps_cmd, timeout=20)
    if out:
        if "YES" in out:
            return True
        if "NO" in out:
            return False

    out = _run_in_guest_capture(vmx, r"C:\\Windows\\System32\\quser.exe", timeout=15)
    if out:
        for line in out.splitlines():
            if line.strip().lower().startswith("username"):
                continue
            if " active " in line.lower():
                return True
    return False


def renew_network(vmx: Path, on_progress: callable | None = None) -> None:
    steps: list[tuple[str, list[str]]] = [
        ("IP 해제", [r"C:\\Windows\\System32\\ipconfig.exe", "/release"]),
        ("DHCP 갱신", [r"C:\\Windows\\System32\\ipconfig.exe", "/renew"]),
        ("DNS 플러시", [r"C:\\Windows\\System32\\ipconfig.exe", "/flushdns"]),
    ]

    log = lambda m: (logger.info(m), on_progress and on_progress(m))
    log("네트워크 재협상 시작")
    try:
        wait_for_tools_ready(vmx, timeout=60, probe_interval=0.5, on_progress=on_progress)
    except Exception as e:
        log(f"VMware Tools 준비 확인 실패: {e}")

    for title, cmd in steps:
        log(f"{title} 실행 중…")
        try:
            _run_in_guest(vmx, cmd[0], *cmd[1:], timeout=60, retries=2, success_codes={0,1})
            log(f"{title} 완료")
        except Exception as e:
            log(f"{title} 실패: {e}")
    log("네트워크 재협상 종료")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Strict dependency check: psutil is required for reliable CPU metrics
    try:
        import psutil  # noqa: F401
    except Exception as e:
        logger.error("Missing required dependency: psutil. Please install requirements.txt. (%s)", e)
        sys.exit(1)

    # Start watchdog thread at startup (forced ON)
    policy = IdlePolicy(
        enabled=True,
        idle_minutes=5,
        check_interval_sec=IDLE_CHECK_INTERVAL_SEC,
        mode=IDLE_SHUTDOWN_MODE,
    )
    t = threading.Thread(target=_watchdog_loop, args=(policy,), daemon=True)
    t.start()
    logger.info("Idle watchdog 스레드 시작됨 (forced ON)")
    yield
    # No teardown required


app = FastAPI(title="QA VMware API", version="1.0.0", lifespan=lifespan)


def _run_vmrun(args: List[str], capture: bool = True, timeout: int = 120) -> str:
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
        print(f"vmrun 타임아웃 ({timeout}초): {' '.join(cmd)}")
        try:
            exc.process.kill()
            print("vmrun 프로세스 강제 종료됨")
        except Exception:
            pass
        return ""
    except subprocess.CalledProcessError as exc:
        logger.error("vmrun stderr: %s", exc.stderr.strip())
        logger.error("vmrun stdout: %s", exc.stdout.strip())
        logger.error("vmrun cmd: %s", ' '.join(cmd))
        raise RuntimeError(f"vmrun failed: {exc.stderr.strip()}") from exc


def _get_host_available_memory_gb() -> float:
    """Return host available physical memory in GB (best-effort).

    - On Windows, use GlobalMemoryStatusEx via ctypes.
    - Else, try psutil if available. On failure, return a large value to avoid false pressure.
    """
    try:
        if os.name == "nt":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return float(stat.ullAvailPhys) / (1024 ** 3)
        try:
            import psutil  # type: ignore
            return float(psutil.virtual_memory().available) / (1024 ** 3)
        except Exception:
            return 9999.0
    except Exception:
        return 9999.0


def _is_pressure_high() -> tuple[bool, float, float]:
    global _CPU_OVER_LIMIT_COUNT
    avail = _get_host_available_memory_gb()
    cpu_pct = _get_host_cpu_percent()
    mem_pressure = avail < MIN_AVAILABLE_MEM_GB
    if cpu_pct >= CPU_PRESSURE_THRESHOLD_PCT:
        _CPU_OVER_LIMIT_COUNT += 1
    else:
        _CPU_OVER_LIMIT_COUNT = 0
    cpu_pressure = _CPU_OVER_LIMIT_COUNT >= max(1, CPU_CONSECUTIVE_TICKS)
    return ((mem_pressure or cpu_pressure), avail, cpu_pct)


def _get_host_cpu_percent() -> float:
    """Return recent CPU usage percent across all cores (best-effort).

    Order: psutil (preferred) → PowerShell Get-Counter → typeperf. Fallback 0.
    Handles locale decimals.
    """
    # 1) psutil if available
    try:
        import psutil  # type: ignore
        pct = float(psutil.cpu_percent(interval=CPU_SAMPLE_DURATION_SEC))
        if pct >= 0.0:
            return pct
    except Exception:
        pass

    if os.name == "nt":
        # 2) PowerShell Get-Counter
        try:
            ps_cmd = (
                "($s=(Get-Counter '" + r"\Processor(_Total)\% Processor Time" + "' -SampleInterval 1 -MaxSamples 1).CounterSamples.CookedValue) | "
                "Measure-Object -Average | ForEach-Object { [int][math]::Round($_.Average) }"
            )
            proc = subprocess.run([
                r"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
                "-NoProfile",
                "-Command",
                ps_cmd,
            ], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                out = proc.stdout.strip()
                if out.isdigit():
                    return float(int(out))
        except Exception:
            pass

        # 3) typeperf CSV
        try:
            cmd = ["typeperf", "-sc", "1", r"\Processor(_Total)\% Processor Time"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                if len(lines) >= 3:
                    last = lines[-1]
                    import re as _re
                    m = _re.search(r"([0-9]+[\.,][0-9]+|[0-9]+)$", last)
                    if m:
                        val_str = m.group(1).replace(",", ".")
                        return float(val_str)
        except Exception:
            pass

    return 0.0


 


def vmx_from_name(name: str) -> Path:
    if name in VM_MAP:
        return VM_MAP[name]
    try:
        from vm_discovery import find_vmx_for_name
        vmx = find_vmx_for_name(name, VM_ROOT)
        if vmx is not None:
            return vmx
    except Exception:
        pass
    raise HTTPException(404, detail=f"Unknown VM '{name}'")


def is_vm_running(vmx: Path) -> bool:
    try:
        status = _run_vmrun(["list"], timeout=10)
        return str(vmx) in status
    except Exception as e:
        print(f"VM 상태 확인 중 오류: {e}")
        return False


def list_snapshots(vmx: Path) -> List[str]:
    raw = _run_vmrun(["listSnapshots", str(vmx)])
    return [ln.strip() for ln in raw.splitlines()[1:]]


def start_vm_async(vmx: Path) -> None:
    import threading

    def run_start_command():
        try:
            print("VM 시작 명령 실행 중...")
            subprocess.Popen(
                [str(VMRUN), "-T", "ws", "start", str(vmx), "nogui"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            print("VM 시작 명령 전송 완료")
        except Exception as e:
            print(f"VM 시작 명령 실행 중 오류: {e}")

    threading.Thread(target=run_start_command, daemon=True).start()


def _is_preferred_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if any(ip_obj in net for net in EXCLUDE_SUBNETS):
        return False
    return any(ip_obj in net for net in PREFERRED_SUBNETS)

def _tools_ready(vmx: Path) -> bool:
    try:
        state = _run_vmrun(["checkToolsState", str(vmx)], timeout=10)
        return "running" in state.lower()
    except Exception:
        return False


def wait_for_tools_ready(vmx: Path, timeout: int = 60, probe_interval: float = 0.1, on_progress: callable | None = None) -> None:
    start = time.perf_counter()
    log = lambda m: (logger.info(m), on_progress and on_progress(m))
    while True:
        if _tools_ready(vmx):
            log("VMware Tools 준비 완료")
            return
        if time.perf_counter() - start > timeout:
            raise TimeoutError("VMware Tools 준비 타임아웃")
        time.sleep(probe_interval)


def fast_wait_for_ip(
    vmx: Path,
    timeout: int = 60,
    probe_interval: float = 0.2,
    on_progress: callable | None = None,
) -> str:
    start_time = time.perf_counter()
    last_ip = ""
    log = lambda m: (logger.info(m), on_progress and on_progress(m))
    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError("fast_wait_for_ip: 시간 초과")
        try:
            raw = _run_vmrun(["getGuestIPAddress", str(vmx)], capture=True, timeout=10)
            ip = raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if ip != last_ip:
                    log(f"IP 확인: {ip}")
                    last_ip = ip
                return ip
        except Exception:
            pass
        time.sleep(probe_interval)


def wait_for_vm_ready(
    vmx: Path,
    timeout: int | None = None,
    probe_interval: float | None = None,
    on_progress: callable | None = None,
    rdp_port: int = 3389,
) -> str:
    if timeout is None:
        timeout = IP_POLL_TIMEOUT
    if probe_interval is None:
        probe_interval = IP_POLL_INTERVAL

    log = lambda m: (print(m, flush=True), on_progress and on_progress(m))
    log(f"VM 준비 상태 확인 중... (최대 {timeout}초, 주기 {probe_interval}초)")

    start_check_deadline = time.perf_counter() + 2
    while time.perf_counter() < start_check_deadline:
        if is_vm_running(vmx):
            log("VM 프로세스 시작 감지")
            break
        time.sleep(probe_interval)

    start_time = time.perf_counter()
    last_ip = ""

    def _log(msg: str):
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    def _ping_ok(host: str) -> bool:
        try:
            res = subprocess.run(["ping", "-n", "1", "-w", "600", host], capture_output=True, text=True, timeout=2)
            return "TTL=" in res.stdout
        except Exception:
            return False

    while True:
        if int(time.perf_counter() - start_time) > timeout:
            raise TimeoutError(f"{timeout}초 내에 유효한 IP를 확인하지 못했습니다")
        try:
            ip_raw = _run_vmrun(["getGuestIPAddress", str(vmx)], capture=True, timeout=10)
            ip = ip_raw.strip()
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                if ip != last_ip:
                    _log(f"IP 취득 중: {ip}")
                    last_ip = ip
                if not _ping_ok(ip):
                    _log("핑 응답 없음 – 네트워크 재협상")
                    renew_network(vmx, on_progress=on_progress)
                    continue

                _log("IP 검증 완료!")
                return ip
        except Exception:
            pass
        time.sleep(probe_interval)


def revert_to_snapshot(vmx: Path, snap: str, on_progress: callable | None = None) -> None:
    log = lambda m: (print(m, flush=True), on_progress and on_progress(m))
    log(f"스냅샷 복구 중: {snap}")
    _run_vmrun(["revertToSnapshot", str(vmx), snap], timeout=60)
    log("스냅샷 복구 완료")
    log("VM 상태 확인 중...")
    time.sleep(0.2)
    if is_vm_running(vmx):
        log("VM이 이미 실행 중입니다.")
        return
    log("VM 시작 중...")
    start_vm_async(vmx)
    time.sleep(0.2)


def get_guest_ip(vmx: Path, timeout: int = IP_POLL_TIMEOUT) -> str:
    return wait_for_vm_ready(vmx, timeout)


class SnapshotListResponse(BaseModel):
    vm: str
    snapshots: List[str] = Field(..., description="스냅샷 이름 배열")


class RevertRequest(BaseModel):
    snapshot: str
    vm: str = "init"


class RevertResponse(BaseModel):
    vm: str
    snapshot: str
    ip: str


class ConnectRequest(BaseModel):
    vm: str = "init"


class ConnectResponse(BaseModel):
    vm: str
    ip: str


class ExpectedTimeResponse(BaseModel):
    vm: str
    op: str
    avg_seconds: float | None


class TaskInfo(BaseModel):
    status: str
    progress: str = "대기 중"
    ip: str | None = None
    started: float | None = None
    finished: float | None = None
    error: str | None = None


TASKS: dict[str, TaskInfo] = {}
_IDLE_DB: dict[str, IdleState] = {}


class VMListItem(BaseModel):
    name: str
    vmx: str


class VMListResponse(BaseModel):
    root: str
    vms: List[VMListItem]


class IdlePolicy(BaseModel):
    enabled: bool = Field(default=False, description="Enable idle shutdown watchdog")
    idle_minutes: int = Field(default=5, description="Minutes of no RDP activity before shutdown (0 = immediate)")
    check_interval_sec: int = Field(default=60, description="Watchdog tick interval seconds")
    mode: str = Field(default="soft", description="Shutdown mode: soft|hard")


class ResourcePolicy(BaseModel):
    min_available_mem_gb: float = Field(default=6.0, description="When host available memory (GB) falls below this, reclaim idle VMs")
    max_shutdowns_per_tick: int = Field(default=2, description="Max number of VMs to stop in a single sweep")
    cpu_pressure_threshold_pct: int = Field(default=95, description="When CPU usage exceeds this percent, treat as pressure")
    cpu_consecutive_ticks: int = Field(default=3, description="Number of consecutive ticks above threshold required to trigger CPU pressure")


def _select_idle_vms_for_stop(candidates: list[Path]) -> list[Path]:
    if not candidates:
        return []
    scored: list[tuple[float, Path]] = []
    now = time.time()
    for vmx in candidates:
        key = str(vmx)
        st = _IDLE_DB.get(key)
        last = st.last_active_ts if st and st.last_active_ts is not None else 0.0
        scored.append((last, vmx))
    scored.sort(key=lambda x: (x[0], str(x[1]).lower()))
    return [vmx for _, vmx in scored[: max(1, MAX_SHUTDOWNS_PER_TICK)]]


class IdleState(BaseModel):
    vm: str
    vmx: str
    last_active_ts: float | None = None
    shutting_down: bool = False


@app.get("/vms", response_model=VMListResponse)
def list_vms() -> VMListResponse:
    def _fallback_discover(root: Path) -> dict[str, Path]:
        mapping: dict[str, Path] = {}
        if not root.exists() or not root.is_dir():
            return mapping
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            # prefer vmx stem == dirname (space-insensitive)
            dir_key = entry.name.lower().replace(" ", "")
            candidates = sorted(entry.rglob("*.vmx"))
            if not candidates:
                continue
            chosen = None
            for vmx in candidates:
                if vmx.stem.lower().replace(" ", "") == dir_key:
                    chosen = vmx
                    break
            if chosen is None:
                chosen = candidates[0]
            mapping[entry.name] = chosen
        return mapping

    try:
        from vm_discovery import discover_vms
        mapping = discover_vms(VM_ROOT)
    except Exception:
        mapping = _fallback_discover(VM_ROOT)
    items = [VMListItem(name=k, vmx=str(v)) for k, v in mapping.items()]
    return VMListResponse(root=str(VM_ROOT), vms=items)


def _shutdown_vm(vmx: Path, mode: str = "soft") -> None:
    try:
        if mode == "hard":
            _run_vmrun(["stop", str(vmx), "hard"], timeout=30)
        else:
            _run_vmrun(["stop", str(vmx), "soft"], timeout=60)
    except Exception as e:
        logger.warning("VM 종료 실패(%s): %s", mode, e)


def _watchdog_tick(policy: IdlePolicy) -> None:
    try:
        running_raw = _run_vmrun(["list"], timeout=10)
        lines = [ln.strip() for ln in running_raw.splitlines() if ln.strip()]
        vmx_list: list[Path] = []
        for ln in lines[1:]:
            p = Path(ln)
            if p.suffix.lower() == ".vmx" and p.exists():
                vmx_list.append(p)
    except Exception:
        vmx_list = []

    now = time.time()
    threshold_seconds = max(0, int(policy.idle_minutes) * 60)

    # If no pressure, do not stop VMs; just maintain last_active timestamps
    pressure, avail, cpu_pct = _is_pressure_high()
    if not pressure:
        for vmx in vmx_list:
            active = has_active_rdp_connections(vmx, rdp_port=RDP_PORT)
            key = str(vmx)
            name = vmx.parent.name
            if active:
                _IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)
        cpu_free_pct = max(0.0, 100.0 - float(cpu_pct))
        logger.info("자원 여유 충분(free_mem=%.1fGB, free_cpu=%.0f%%) – 종료 없음", avail, cpu_free_pct)
        return

    for vmx in vmx_list:
        name = None
        for k, v in VM_MAP.items():
            if v == vmx:
                name = k
                break
        if name is None:
            name = vmx.parent.name

        active = has_active_rdp_connections(vmx, rdp_port=RDP_PORT)
        key = str(vmx)
        state = _IDLE_DB.get(key)
        if active:
            _IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now, shutting_down=False)
            continue

        # No active connection
        if threshold_seconds == 0:
            # immediate mode still requires pressure high per new policy
            continue

        if state is None or state.last_active_ts is None:
            _IDLE_DB[key] = IdleState(vm=name, vmx=str(vmx), last_active_ts=now)
            continue

        idle_secs = now - state.last_active_ts
        if idle_secs >= threshold_seconds and not state.shutting_down:
            # never stop VMs that currently have any RDP connection
            candidates = [v for v in vmx_list if not has_active_rdp_connections(v, rdp_port=RDP_PORT)]
            to_stop = _select_idle_vms_for_stop(candidates)
            for victim in to_stop:
                key2 = str(victim)
                s2 = _IDLE_DB.get(key2) or IdleState(vm=victim.parent.name, vmx=str(victim), last_active_ts=now)
                logger.info("자원 압박(free_mem=%.1fGB, used_cpu=%.0f%%) – 유휴 VM 종료: %s", avail, cpu_pct, victim)
                _IDLE_DB[key2] = IdleState(vm=s2.vm, vmx=s2.vmx, last_active_ts=s2.last_active_ts, shutting_down=True)
                _shutdown_vm(victim, mode=policy.mode)
            break


def _watchdog_loop(policy: IdlePolicy) -> None:
    logger.info("Idle watchdog 시작 – enabled=%s, idle=%dm, interval=%ss, mode=%s, mem_threshold=%.1fGB, cpu_threshold=%d%%@%dx, maxStops=%d",
                policy.enabled, policy.idle_minutes, policy.check_interval_sec, policy.mode,
                MIN_AVAILABLE_MEM_GB, CPU_PRESSURE_THRESHOLD_PCT, CPU_CONSECUTIVE_TICKS, MAX_SHUTDOWNS_PER_TICK)
    while policy.enabled:
        try:
            _watchdog_tick(policy)
        except Exception as e:
            logger.warning("watchdog tick 오류: %s", e)
        time.sleep(max(5, policy.check_interval_sec))


@app.get("/snapshots", response_model=SnapshotListResponse)
def snapshots(vm: str = "init") -> SnapshotListResponse:
    try:
        vmx = vmx_from_name(vm)
        snaps = list_snapshots(vmx)
        return SnapshotListResponse(vm=vm, snapshots=snaps)
    except Exception:
        import traceback
        traceback.print_exc()
        raise


@app.post("/revert", response_model=RevertResponse)
def revert(payload: RevertRequest) -> RevertResponse:
    start_ts = time.perf_counter()
    try:
        vmx = vmx_from_name(payload.vm)
        snaps = list_snapshots(vmx)
        if payload.snapshot not in snaps:
            raise HTTPException(404, f"Snapshot '{payload.snapshot}' not found.")
        revert_to_snapshot(vmx, payload.snapshot)
        probe, tout = _calc_poll_params(payload.vm, "revert")
        fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe)
        renew_network(vmx)
        ip_addr = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe)
        record_duration(f"{payload.vm}_revert", time.perf_counter() - start_ts)
        return RevertResponse(vm=payload.vm, snapshot=payload.snapshot, ip=ip_addr)
    except Exception:
        import traceback
        traceback.print_exc()
        raise


def _revert_job(vm: str, snap: str, task_id: str):
    task = TASKS[task_id]
    try:
        task.status = "running"
        task.started = time.time()
        task.progress = "스냅샷 복구 중"
        vmx = vmx_from_name(vm)
        revert_to_snapshot(vmx, snap, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "IP 획득 중 "
        stage_start = time.perf_counter()
        probe, tout = _calc_poll_params(vm, "revert")
        ip = fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe, on_progress=lambda msg: setattr(task, "progress", msg))
        task.progress = f"IP(1차)={ip} – 네트워크 재협상 중"
        renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "IP 재확인(2차)"
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda msg: setattr(task, "progress", msg))
        try:
            socket.create_connection((ip, 3389), timeout=3).close()
        except Exception:
            task.progress = "RDP 대기 초과 – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda msg: setattr(task, "progress", msg))
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda msg: setattr(task, "progress", msg))
        task.ip = ip
        task.status = "done"
        task.progress = "완료"
        task.finished = time.time()
        record_duration(f"{vm}_revert", task.finished - task.started)
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        task.finished = time.time()


def _connect_job(vm: str, task_id: str):
    task = TASKS[task_id]
    try:
        task.status = "running"
        task.started = time.time()
        task.progress = "IP 획득 중"
        vmx = vmx_from_name(vm)
        if not is_vm_running(vmx):
            start_vm_async(vmx)
        stage_start = time.perf_counter()
        probe, tout = _calc_poll_params(vm, "connect")
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda msg: setattr(task, "progress", msg))
        if not _is_preferred_ip(ip):
            task.progress = "예상치 않은 IP – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda msg: setattr(task, "progress", msg))
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda msg: setattr(task, "progress", msg))
        record_duration(f"{vm}_stage_wait", time.perf_counter() - stage_start)
        task.ip = ip
        task.status = "done"
        task.progress = "완료"
        task.finished = time.time()
        record_duration(f"{vm}_connect", task.finished - task.started)
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        task.finished = time.time()


@app.post("/revert_async")
def revert_async(payload: RevertRequest, bg: BackgroundTasks):
    tid = str(uuid4())
    TASKS[tid] = TaskInfo(status="queued")
    bg.add_task(_revert_job, payload.vm, payload.snapshot, tid)
    return {"task_id": tid}


@app.post("/connect_async")
def connect_async(payload: ConnectRequest, bg: BackgroundTasks):
    tid = str(uuid4())
    TASKS[tid] = TaskInfo(status="queued")
    bg.add_task(_connect_job, payload.vm, tid)
    return {"task_id": tid}


@app.get("/expected_time", response_model=ExpectedTimeResponse)
def expected_time(vm: str = "init", op: str = "revert") -> ExpectedTimeResponse:
    avg = average_duration(f"{vm}_{op}")
    return ExpectedTimeResponse(vm=vm, op=op, avg_seconds=avg)


@app.get("/task/{task_id}")
def task_status(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(404, "task not found")
    return TASKS[task_id]


@app.get("/idle_policy", response_model=IdlePolicy)
def get_idle_policy() -> IdlePolicy:
    return IdlePolicy(
        enabled=True,
        idle_minutes=5,
        check_interval_sec=IDLE_CHECK_INTERVAL_SEC,
        mode=IDLE_SHUTDOWN_MODE,
    )


@app.get("/resource_policy", response_model=ResourcePolicy)
def get_resource_policy() -> ResourcePolicy:
    return ResourcePolicy(
        min_available_mem_gb=MIN_AVAILABLE_MEM_GB,
        max_shutdowns_per_tick=MAX_SHUTDOWNS_PER_TICK,
        cpu_pressure_threshold_pct=CPU_PRESSURE_THRESHOLD_PCT,
        cpu_consecutive_ticks=CPU_CONSECUTIVE_TICKS,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("qa-vm-api:app", host="0.0.0.0", port=495, workers=1)
