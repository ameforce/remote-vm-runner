from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import socket
import threading
import time
from pathlib import Path

import psutil
from fastapi import BackgroundTasks, FastAPI, HTTPException

from . import config as default_cfg
from . import durations
from .config import (
    IDLE_CHECK_INTERVAL_SEC,
    IDLE_ONLY_ON_PRESSURE,
    IDLE_SHUTDOWN_MODE,
    IP_POLL_INTERVAL,
    IP_POLL_TIMEOUT,
    REQUIRE_GUEST_CREDENTIALS,
    VM_MAP,
    VM_ROOT,
    SKIP_TOOLS_WAIT_WHEN_HEADLESS,
)
from .discovery import discover_vms, find_vmx_for_name
from .idle import IDLE_DB, LAST_STATUS, watchdog_tick
from .models import (
    ConnectRequest,
    ExpectedTimeResponse,
    IdlePolicy,
    RevertRequest,
    RevertResponse,
    ResourcePolicy,
    SnapshotListResponse,
    TaskInfo,
    VMListItem,
    VMListResponse,
)
from .network import has_active_rdp_connections, is_preferred_ip, renew_network
from .vmware import (
    fast_wait_for_ip,
    is_vm_running,
    list_snapshots,
    run_vmrun,
    start_vm_async,
    wait_for_tools_ready,
    wait_for_vm_ready,
    wait_for_rdp_ready,
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
        if not is_vm_running(vmx):
            task.progress = "전원 켜는 중"
            start_vm_async(vmx)
        try:
            if not SKIP_TOOLS_WAIT_WHEN_HEADLESS:
                task.progress = "Tools 대기 중"
                wait_for_tools_ready(vmx, timeout=60, on_progress=lambda m: setattr(task, "progress", m))
        except Exception:
            pass
        task.progress = "IP 획득 중"
        probe, tout = _calc_poll_params(vm, "revert")
        ip = fast_wait_for_ip(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = f"IP(1차)={ip} – 네트워크 재협상 중"
        renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "IP 재확인(2차)"
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "RDP 준비 대기 중"
        if not wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m)):
            task.progress = "RDP 대기 초과 – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            task.progress = "RDP 재대기"
            wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m))
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
        was_running = is_vm_running(vmx)
        if not was_running:
            start_vm_async(vmx)
        probe, tout = _calc_poll_params(vm, "connect")
        ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.progress = "RDP 준비 대기 중"
        if not wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m)):
            task.progress = "RDP 대기 초과 – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            task.progress = "RDP 재대기"
            wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m))
        if not is_preferred_ip(ip):
            task.progress = "예상치 않은 IP – 네트워크 재협상"
            renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
        task.ip = ip
        task.status = "done"
        task.progress = "완료"
        task.finished = time.time()
        key = f"{vm}_connect_warm" if was_running else f"{vm}_connect_cold"
        durations.record_duration(key, task.finished - task.started)
    except Exception as exc:
        task.status = "failed"
        task.error = str(exc)
        task.finished = time.time()


def create_app(config_module=None) -> FastAPI:
    cfg = config_module or default_cfg

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = logging.getLogger("src.api")
        policy = IdlePolicy(
            enabled=True,
            idle_minutes=5,
            check_interval_sec=IDLE_CHECK_INTERVAL_SEC,
            mode=IDLE_SHUTDOWN_MODE,
            only_on_pressure=IDLE_ONLY_ON_PRESSURE,
        )
        log.info("starting watchdog thread: interval=%ss idle_minutes=%s mode=%s", policy.check_interval_sec, policy.idle_minutes, policy.mode)
        t = threading.Thread(target=_watchdog_loop, args=(policy,), daemon=True)
        t.start()
        yield

    app = FastAPI(title="QA VMware API", version="1.0.0", lifespan=lifespan)

    _log = logging.getLogger("src.api")
    _u = (os.getenv("GUEST_USER") or "").strip()
    _p = (os.getenv("GUEST_PASS") or "").strip()
    if not _u or not _p:
        _log.warning("Guest credentials are not fully set in environment: GUEST_USER=%s, GUEST_PASS=%s", bool(_u), bool(_p))
        if REQUIRE_GUEST_CREDENTIALS:
            _log.error("REQUIRE_GUEST_CREDENTIALS=true and required env vars are missing; refusing to start.")
            raise RuntimeError("Missing required environment variables: GUEST_USER/GUEST_PASS")

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

    @app.get("/vm_state")
    def vm_state(vm: str = "init"):
        vmx = _vmx_from_name_local(vm)
        running = is_vm_running(vmx)
        return {"vm": vm, "running": bool(running)}

    @app.post("/revert", response_model=RevertResponse)
    def revert(payload: RevertRequest) -> RevertResponse:
        start_ts = time.perf_counter()
        vmx = _vmx_from_name_local(payload.vm)
        snaps = list_snapshots(vmx)
        if payload.snapshot not in snaps:
            raise HTTPException(404, f"Snapshot '{payload.snapshot}' not found.")
        run_vmrun(["revertToSnapshot", str(vmx), payload.snapshot], timeout=60)
        if not is_vm_running(vmx):
            start_vm_async(vmx)
        try:
            if not SKIP_TOOLS_WAIT_WHEN_HEADLESS:
                wait_for_tools_ready(vmx, timeout=60)
        except Exception:
            pass
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
            if not is_vm_running(vmx):
                task.progress = "전원 켜는 중"
                start_vm_async(vmx)
            try:
                task.progress = "Tools 대기 중"
                wait_for_tools_ready(vmx, timeout=60, on_progress=lambda m: setattr(task, "progress", m))
            except Exception:
                pass
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
            was_running = is_vm_running(vmx)
            if not was_running:
                start_vm_async(vmx)
            probe, tout = _calc_poll_params(vm, "connect")
            ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            task.progress = "RDP 준비 대기 중"
            if not wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m)):
                task.progress = "RDP 대기 초과 – 네트워크 재협상"
                renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
                task.progress = "RDP 재대기"
                wait_for_rdp_ready(vmx, ip, on_progress=lambda m: setattr(task, "progress", m))
            if not is_preferred_ip(ip):
                task.progress = "예상치 않은 IP – 네트워크 재협상"
                renew_network(vmx, on_progress=lambda m: setattr(task, "progress", m))
                ip = wait_for_vm_ready(vmx, timeout=tout, probe_interval=probe, on_progress=lambda m: setattr(task, "progress", m))
            task.ip = ip
            task.status = "done"
            task.progress = "완료"
            task.finished = time.time()
            key = f"{vm}_connect_warm" if was_running else f"{vm}_connect_cold"
            durations.record_duration(key, task.finished - task.started)
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
            only_on_pressure=IDLE_ONLY_ON_PRESSURE,
        )

    @app.get("/resource_policy", response_model=ResourcePolicy)
    def get_resource_policy() -> ResourcePolicy:
        return ResourcePolicy()

    @app.get("/guest_credentials")
    def get_guest_credentials():
        log = logging.getLogger("src.api")
        user = (os.getenv("GUEST_USER") or "").strip()
        pw = (os.getenv("GUEST_PASS") or "").strip()
        if not user:
            log.warning("GUEST_USER is not set; clients cannot auto login.")
        if not pw:
            log.warning("GUEST_PASS is not set; clients cannot auto login.")
        return {"guest_user": user, "guest_pass": pw}

    def _watchdog_loop(policy: IdlePolicy) -> None:
        log = logging.getLogger("src.watchdog")
        while policy.enabled:
            try:
                watchdog_tick(policy)
            except Exception as exc:
                try:
                    LAST_STATUS["last_error"] = str(exc)
                except Exception:
                    pass
                log.exception("watchdog tick failed: %s", exc)
            time.sleep(max(5, policy.check_interval_sec))

    @app.get("/health")
    def health():
        status = {
            "ok": True,
            **{k: LAST_STATUS.get(k) for k in ("last_tick_at", "vm_count", "pressure", "available_mem_gb", "cpu_percent", "cpu_used_percent", "cpu_idle_percent", "stopped_count", "interval_sec", "last_error")},
        }
        return status

    return app
