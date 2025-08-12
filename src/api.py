from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
import psutil

from . import durations
from . import config as default_cfg
from .config import (
    IDLE_CHECK_INTERVAL_SEC,
    IDLE_SHUTDOWN_MODE,
    IP_POLL_INTERVAL,
    IP_POLL_TIMEOUT,
    VM_MAP,
    VM_ROOT,
)
from .discovery import discover_vms, find_vmx_for_name
from .idle import IDLE_DB, watchdog_tick
from .models import (
    IdlePolicy,
    ResourcePolicy,
    ConnectRequest,
    ExpectedTimeResponse,
    RevertRequest,
    RevertResponse,
    SnapshotListResponse,
    TaskInfo,
    VMListItem,
    VMListResponse,
)
from .network import has_active_rdp_connections, is_preferred_ip, renew_network
from .vmware import (
    run_vmrun,
    fast_wait_for_ip,
    is_vm_running,
    list_snapshots,
    start_vm_async,
    wait_for_vm_ready,
)


def vmx_from_name(name: str) -> Path:
    if name in VM_MAP:
        return VM_MAP[name]
    vmx = find_vmx_for_name(name, VM_ROOT)
    if vmx is not None:
        return vmx
    raise HTTPException(404, detail=f"Unknown VM '{name}'")


def _calc_poll_params(vm: str, op: str) -> tuple[float, int]:
    return IP_POLL_INTERVAL, IP_POLL_TIMEOUT


TASKS: dict[str, TaskInfo] = {}


def _revert_job(vm: str, snap: str, task_id: str) -> None:
    task = TASKS[task_id]
    try:
        task.status = "running"
        task.started = time.time()
        task.progress = "스냅샷 복구 중"
        vmx = vmx_from_name(vm)
        run_vmrun(["revertToSnapshot", str(vmx), snap], timeout=60)
        task.progress = "IP 획득 중"
        probe, tout = _calc_poll_params(vm, "revert")
        ip = fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = f"IP(1차)={ip} – 네트워크 재협상 중"
        renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "IP 재확인(2차)"
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        try:
            socket.create_connection((ip, 3389), timeout=3).close()
        except Exception:
            task.progress = "RDP 대기 초과 – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.ip = ip
        task.status = "done"
        task.progress = "완료"
        task.finished = time.time()
        durations.record_duration(f"{vm}_revert", task.finished - task.started)
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)
        task.finished = time.time()


def _connect_job(vm: str, task_id: str) -> None:
    task = TASKS[task_id]
    try:
        task.status = "running"
        task.started = time.time()
        task.progress = "IP 획득 중"
        vmx = vmx_from_name(vm)
        if not is_vm_running(vmx):
            start_vm_async(vmx)
        probe, tout = _calc_poll_params(vm, "connect")
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        if not is_preferred_ip(ip):
            task.progress = "예상치 않은 IP – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.ip = ip
        task.status = "done"
        task.progress = "완료"
        task.finished = time.time()
        durations.record_duration(f"{vm}_connect", task.finished - task.started)
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)
        task.finished = time.time()


def create_app(config_module=None) -> FastAPI:
    cfg = config_module or default_cfg

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        policy = IdlePolicy(
            enabled=True,
            idle_minutes=5,
            check_interval_sec=IDLE_CHECK_INTERVAL_SEC,
            mode=IDLE_SHUTDOWN_MODE,
        )
        t = threading.Thread(target=_watchdog_loop, args=(policy,), daemon=True)
        t.start()
        yield

    app = FastAPI(title="QA VMware API", version="1.0.0", lifespan=lifespan)

    @app.get("/vms", response_model=VMListResponse)
    def list_vms() -> VMListResponse:
        try:
            mapping = discover_vms(cfg.VM_ROOT)
        except Exception:
            mapping = {}
        items = [VMListItem(name=k, vmx=str(v)) for k, v in mapping.items()]
        return VMListResponse(root=str(cfg.VM_ROOT), vms=items)

    def _vmx_from_name_local(name: str) -> Path:
        if name in getattr(cfg, "VM_MAP", {}):
            return cfg.VM_MAP[name]
        vmx = find_vmx_for_name(name, cfg.VM_ROOT)
        if vmx is not None:
            return vmx
        raise HTTPException(404, detail=f"Unknown VM '{name}'")

    @app.get("/snapshots", response_model=SnapshotListResponse)
    def snapshots(vm: str = "init") -> SnapshotListResponse:
        vmx = _vmx_from_name_local(vm)
        snaps = list_snapshots(vmx)
        return SnapshotListResponse(vm=vm, snapshots=snaps)

    @app.post("/revert", response_model=RevertResponse)
    def revert(payload: RevertRequest) -> RevertResponse:
        start_ts = time.perf_counter()
        vmx = _vmx_from_name_local(payload.vm)
        snaps = list_snapshots(vmx)
        if payload.snapshot not in snaps:
            raise HTTPException(404, f"Snapshot '{payload.snapshot}' not found.")
        run_vmrun(["revertToSnapshot", str(vmx), payload.snapshot], timeout=60)
        probe, tout = _calc_poll_params(payload.vm, "revert")
        fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe)
        renew_network(vmx)
        ip_addr = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe)
        durations.record_duration(f"{payload.vm}_revert", time.perf_counter() - start_ts)
        return RevertResponse(vm=payload.vm, snapshot=payload.snapshot, ip=ip_addr)

    def _revert_job_local(vm: str, snap: str, task_id: str) -> None:
        task = TASKS[task_id]
        try:
            task.status = "running"
            task.started = time.time()
            task.progress = "스냅샷 복구 중"
            vmx = _vmx_from_name_local(vm)
            run_vmrun(["revertToSnapshot", str(vmx), snap], timeout=60)
            task.progress = "IP 획득 중"
            probe, tout = _calc_poll_params(vm, "revert")
            ip = fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            task.progress = f"IP(1차)={ip} – 네트워크 재협상 중"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            task.progress = "IP 재확인(2차)"
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            try:
                socket.create_connection((ip, 3389), timeout=3).close()
            except Exception:
                task.progress = "RDP 대기 초과 – 네트워크 재협상"
                renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
                ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            task.ip = ip
            task.status = "done"
            task.progress = "완료"
            task.finished = time.time()
            durations.record_duration(f"{vm}_revert", task.finished - task.started)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished = time.time()

    @app.post("/revert_async")
    def revert_async(payload: RevertRequest, bg: BackgroundTasks):
        tid = str(time.time())
        TASKS[tid] = TaskInfo(status="queued")
        bg.add_task(_revert_job_local, payload.vm, payload.snapshot, tid)
        return {"task_id": tid}

    def _connect_job_local(vm: str, task_id: str) -> None:
        task = TASKS[task_id]
        try:
            task.status = "running"
            task.started = time.time()
            task.progress = "IP 획득 중"
            vmx = _vmx_from_name_local(vm)
            if not is_vm_running(vmx):
                start_vm_async(vmx)
            probe, tout = _calc_poll_params(vm, "connect")
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            if not is_preferred_ip(ip):
                task.progress = "예상치 않은 IP – 네트워크 재협상"
                renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
                ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            task.ip = ip
            task.status = "done"
            task.progress = "완료"
            task.finished = time.time()
            durations.record_duration(f"{vm}_connect", task.finished - task.started)
        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished = time.time()

    @app.post("/connect_async")
    def connect_async(payload: ConnectRequest, bg: BackgroundTasks):
        tid = str(time.time())
        TASKS[tid] = TaskInfo(status="queued")
        bg.add_task(_connect_job_local, payload.vm, tid)
        return {"task_id": tid}

    @app.get("/expected_time", response_model=ExpectedTimeResponse)
    def expected_time(vm: str = "init", op: str = "revert") -> ExpectedTimeResponse:
        avg = durations.average_duration(f"{vm}_{op}")
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
        return ResourcePolicy()

    def _watchdog_loop(policy: IdlePolicy) -> None:
        while policy.enabled:
            try:
                watchdog_tick(policy)
            except Exception:
                pass
            time.sleep(max(5, policy.check_interval_sec))

    return app
