from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable

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
    RDP_PORT,
    VM_MAP,
    VM_ROOT,
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
from .network import is_preferred_ip, renew_network
from .rdp_probe import (
    get_active_rdp_remote_ips,
    get_active_rdp_usernames,
    get_active_rdp_usernames_best,
    has_active_rdp_connections_tcp,
    probe_rdp_usage,
)
from .vmware import fast_wait_for_ip, is_vm_running, list_snapshots, run_vmrun, ensure_vm_running, wait_for_vm_ready, wait_for_rdp_ready, tools_ready


def _calc_poll_params(vm: str, op: str) -> tuple[float, int]:
    return IP_POLL_INTERVAL, IP_POLL_TIMEOUT


TASKS: dict[str, TaskInfo] = {}


ProgressCallback = Callable[[str], None] | None


def _safe_progress(cb: ProgressCallback, message: str) -> None:
    if not cb or not message:
        return
    try:
        cb(message)
    except Exception:
        pass


def _progress_adapter(cb: ProgressCallback) -> Callable[[str], None]:
    def _inner(message: str) -> None:
        _safe_progress(cb, message)

    return _inner


def _task_progress_writer(task: TaskInfo) -> Callable[[str], None]:
    def _update(message: str) -> None:
        if not message:
            return
        try:
            task.progress = message
        except Exception:
            pass

    return _update


def _active_rdp_clients(vmx: Path) -> list[str]:
    try:
        return get_active_rdp_remote_ips(vmx)
    except Exception:
        return []


def _run_revert_pipeline(
    vmx: Path,
    _vm: str,
    snapshot: str,
    probe_interval: float,
    timeout: int,
    progress: ProgressCallback,
) -> str:
    progress_cb = _progress_adapter(progress)
    run_vmrun(["revertToSnapshot", str(vmx), snapshot], timeout=60)
    ensure_vm_running(vmx, timeout=60, on_progress=progress_cb)
    try:
        _safe_progress(progress, "Tools 상태 단일 확인")
        time.sleep(3.0)
        if tools_ready(vmx):
            _safe_progress(progress, "VMware Tools 준비 완료(단일 체크)")
    except Exception:
        pass
    _safe_progress(progress, "IP 획득 중")
    fast_ip = fast_wait_for_ip(
        vmx,
        timeout=timeout,
        probe_interval=probe_interval,
        on_progress=progress_cb,
    )
    _safe_progress(progress, f"IP(1차)={fast_ip} – 네트워크 재협상 중")
    renew_network(vmx, on_progress=progress_cb)
    _safe_progress(progress, "IP 재확인(2차)")
    ip = wait_for_vm_ready(
        vmx,
        timeout=timeout,
        probe_interval=probe_interval,
        on_progress=progress_cb,
    )
    _safe_progress(progress, "RDP 준비 대기 중")
    if not wait_for_rdp_ready(vmx, ip, on_progress=progress_cb):
        _safe_progress(progress, "RDP 대기 초과 – 네트워크 재협상")
        renew_network(vmx, on_progress=progress_cb)
        _safe_progress(progress, "RDP 재대기")
        wait_for_rdp_ready(vmx, ip, on_progress=progress_cb)
    try:
        socket.create_connection((ip, RDP_PORT), timeout=3).close()
    except Exception:
        _safe_progress(progress, "RDP 대기 초과 – 네트워크 재협상")
        renew_network(vmx, on_progress=progress_cb)
        _safe_progress(progress, "IP 재확인(2차)")
        ip = wait_for_vm_ready(
            vmx,
            timeout=timeout,
            probe_interval=probe_interval,
            on_progress=progress_cb,
        )
    return ip


def _run_connect_pipeline(
    vmx: Path,
    _vm: str,
    probe_interval: float,
    timeout: int,
    progress: ProgressCallback,
) -> tuple[str, bool]:
    progress_cb = _progress_adapter(progress)
    try:
        initial_running = bool(is_vm_running(vmx))
    except Exception:
        initial_running = False
    _safe_progress(progress, "전원 상태 확인 중")
    ensure_vm_running(vmx, timeout=60, on_progress=progress_cb)
    try:
        _safe_progress(progress, "Tools 상태 단일 확인")
        time.sleep(3.0)
        if tools_ready(vmx):
            _safe_progress(progress, "VMware Tools 준비 완료(단일 체크)")
    except Exception:
        pass
    _safe_progress(progress, "IP 획득 중")
    ip = wait_for_vm_ready(
        vmx,
        timeout=timeout,
        probe_interval=probe_interval,
        on_progress=progress_cb,
    )
    _safe_progress(progress, "RDP 준비 대기 중")
    if not wait_for_rdp_ready(vmx, ip, on_progress=progress_cb):
        _safe_progress(progress, "RDP 대기 초과 – 네트워크 재협상")
        renew_network(vmx, on_progress=progress_cb)
        _safe_progress(progress, "RDP 재대기")
        wait_for_rdp_ready(vmx, ip, on_progress=progress_cb)
    if not is_preferred_ip(ip):
        _safe_progress(progress, "예상치 않은 IP – 네트워크 재협상")
        renew_network(vmx, on_progress=progress_cb)
        ip = wait_for_vm_ready(
            vmx,
            timeout=timeout,
            probe_interval=probe_interval,
            on_progress=progress_cb,
        )
    return ip, initial_running


def create_app(config_module=None) -> FastAPI:
    cfg = config_module or default_cfg

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = logging.getLogger("src.api")
        policy = IdlePolicy(
            enabled=True,
            check_interval_sec=IDLE_CHECK_INTERVAL_SEC,
            mode=IDLE_SHUTDOWN_MODE,
            only_on_pressure=IDLE_ONLY_ON_PRESSURE,
        )
        log.info("starting watchdog thread: interval=%ss mode=%s", policy.check_interval_sec, policy.mode)
        t = threading.Thread(target=_watchdog_loop, args=(policy,), daemon=True)
        t.start()
        yield

    app = FastAPI(title="QA VMware API", version="1.0.0", lifespan=lifespan)

    _log = logging.getLogger("src.api")
    try:
        logging.getLogger().setLevel(logging.DEBUG)
        _log.setLevel(logging.DEBUG)
        logging.getLogger("src").setLevel(logging.DEBUG)
        logging.getLogger("src.rdpmon").setLevel(logging.DEBUG)
        logging.getLogger("src.watchdog").setLevel(logging.DEBUG)
    except Exception:
        pass
    _u = (os.getenv("GUEST_USER") or "").strip()
    _p = (os.getenv("GUEST_PASS") or "").strip()
    if not _u or not _p:
        _log.warning("Guest credentials are not fully set in environment: GUEST_USER=%s, GUEST_PASS=%s", bool(_u), bool(_p))
        if REQUIRE_GUEST_CREDENTIALS:
            _log.error("REQUIRE_GUEST_CREDENTIALS=true and required env vars are missing; refusing to start.")
            raise RuntimeError("Missing required environment variables: GUEST_USER/GUEST_PASS")

    @app.get("/vms", response_model=VMListResponse)
    def list_vms(include_active: bool = True) -> VMListResponse:
        try:
            mapping = discover_vms(cfg.VM_ROOT)
        except Exception:
            mapping = {}
        items: list[VMListItem] = []
        for n, vmx in mapping.items():
            items.append(VMListItem(name=n, vmx=str(vmx), clients=[], active=False))
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

    @app.get("/rdp_clients")
    def rdp_clients(vm: str = "init"):
        vmx = _vmx_from_name_local(vm)
        try:
            ips = get_active_rdp_remote_ips(vmx)
        except Exception:
            ips = []
        return {"vm": vm, "clients": ips}

    @app.get("/rdp_active")
    def rdp_active(vm: str = "init"):
        vmx = _vmx_from_name_local(vm)
        try:
            if not is_vm_running(vmx):
                return {"vm": vm, "active": False, "status": "none"}
            active, _clients, status = probe_rdp_usage(vmx)
        except Exception:
            active = False
            status = "error"
        return {"vm": vm, "active": bool(active), "status": status}

    @app.get("/rdp_used")
    def rdp_used(vm: str = "init"):
        vmx = _vmx_from_name_local(vm)
        try:
            if not is_vm_running(vmx):
                return {
                    "vm": vm,
                    "active": False,
                    "clients": [],
                    "status": "none",
                }
            active, clients, status = probe_rdp_usage(vmx)
        except Exception:
            active = False
            clients = []
            status = "error"
        return {
            "vm": vm,
            "active": bool(active),
            "clients": clients,
            "status": status,
        }


    @app.get("/vm_state")
    def vm_state(vm: str = "init"):
        vmx = _vmx_from_name_local(vm)
        running = is_vm_running(vmx)
        return {"vm": vm, "running": bool(running)}

    @app.post("/revert", response_model=RevertResponse)
    def revert(payload: RevertRequest) -> RevertResponse:
        start_ts = time.perf_counter()
        vmx = _vmx_from_name_local(payload.vm)
        active_clients = _active_rdp_clients(vmx)
        if active_clients:
            raise HTTPException(409, detail=f"Active RDP clients detected ({', '.join(active_clients)}); revert is blocked.")
        snaps = list_snapshots(vmx)
        if payload.snapshot not in snaps:
            raise HTTPException(404, f"Snapshot '{payload.snapshot}' not found.")
        probe, tout = _calc_poll_params(payload.vm, "revert")
        ip_addr = _run_revert_pipeline(
            vmx,
            payload.vm,
            payload.snapshot,
            probe,
            tout,
            progress=None,
        )
        durations.record_duration(f"{payload.vm}_revert", time.perf_counter() - start_ts)
        return RevertResponse(vm=payload.vm, snapshot=payload.snapshot, ip=ip_addr)

    def _revert_job_local(vm: str, snap: str, task_id: str) -> None:
        task = TASKS[task_id]
        try:
            task.status = "running"
            task.started = time.time()
            progress_cb = _task_progress_writer(task)
            progress_cb("스냅샷 복구 중")
            vmx = _vmx_from_name_local(vm)
            active_clients = _active_rdp_clients(vmx)
            if active_clients:
                task.status = "failed"
                task.error = f"Active RDP clients detected ({', '.join(active_clients)}); revert is blocked."
                task.finished = time.time()
                return
            probe, tout = _calc_poll_params(vm, "revert")
            ip = _run_revert_pipeline(
                vmx,
                vm,
                snap,
                probe,
                tout,
                progress_cb,
            )
            task.ip = ip
            task.status = "done"
            progress_cb("완료")
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
            progress_cb = _task_progress_writer(task)
            progress_cb("전원 상태 확인 중")
            vmx = _vmx_from_name_local(vm)
            probe, tout = _calc_poll_params(vm, "connect")
            ip, was_running = _run_connect_pipeline(
                vmx,
                vm,
                probe,
                tout,
                progress_cb,
            )
            task.ip = ip
            task.status = "done"
            progress_cb("완료")
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
