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
IP_POLL_INTERVAL = 0.2
IP_POLL_TIMEOUT = 120
_pref_env = os.getenv("PREFERRED_SUBNETS", "192.168.0.0/22")
PREFERRED_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _pref_env.split(',') if net.strip()]
_ex_env = os.getenv("EXCLUDE_SUBNETS", "")
EXCLUDE_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _ex_env.split(',') if net.strip()]

GUEST_USER = os.getenv("GUEST_USER", "administrator")
GUEST_PASS = os.getenv("GUEST_PASS", "epapyrus12#$")


def _run_in_guest(
    vmx: Path,
    program: str,
    *args: str,
    timeout: int = 60,
    retries: int = 3,
) -> None:
    """Guest OS 내부에서 프로그램 실행.

    VMware Tools 가 아직 초기화되지 않았거나 일시적으로 Guest Ops 채널이 끊긴 경우를
    대비해 재시도 로직을 넣었다. 각 재시도 사이에 Tools 준비 여부를 확인한다.
    """

    # vmrun 명령 형식: vmrun -T ws -gu <USER> -gp <PASS> runProgramInGuest <VMX> <PROGRAM> [ARGS]
    # 기존 구현은 -gu/-gp 옵션을 runProgramInGuest 뒤에 배치해 오류가 발생했다.
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

    for attempt in range(1, retries + 1):
        try:
            _run_vmrun(cmd_base, capture=True, timeout=timeout)
            return
        except Exception as e:
            logger.warning("runProgramInGuest 실패(%d/%d): %s", attempt, retries, e)
            if attempt == retries:
                return
            # Tools 준비 재확인 후 잠시 대기하고 재시도
            try:
                wait_for_tools_ready(vmx, timeout=10, probe_interval=0.5)
            except Exception:
                pass
            time.sleep(2)


def renew_network(vmx: Path, on_progress: callable | None = None) -> None:
    """Guest OS 내에서 DHCP 갱신·DNS 플러시를 시도한다.

    각 명령 실행 결과의 stderr 를 캡처해 로깅하고, 실패해도 다음 단계로 넘어간다.
    """
    # runProgramInGuest 에서는 프로그램의 전체 경로 혹은 cmd.exe 로 우회 호출해야 한다.
    steps: list[tuple[str, list[str]]] = [
        ("IP 해제", ["cmd.exe", "/c", "ipconfig", "/release"]),
        ("DHCP 갱신", ["cmd.exe", "/c", "ipconfig", "/renew"]),
        ("DNS 플러시", ["cmd.exe", "/c", "ipconfig", "/flushdns"]),
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
            _run_in_guest(vmx, cmd[0], *cmd[1:], timeout=60, retries=2)
            log(f"{title} 완료")
        except Exception as e:
            log(f"{title} 실패: {e}")
    log("네트워크 재협상 종료")


app = FastAPI(title="QA VMware API", version="1.0.0")


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


def vmx_from_name(name: str) -> Path:
    try:
        return VM_MAP[name]
    except KeyError as exc:
        raise HTTPException(404, detail=f"Unknown VM '{name}'") from exc


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

# VMware Tools 준비 대기
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("qa-vm-api:app", host="0.0.0.0", port=495, workers=1)