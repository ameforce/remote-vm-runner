from __future__ import annotations
from src.cli import VMClient
from src.errors import ExitCode, map_requests_error
from src.envutils import ensure_guest_credentials_interactive, ensure_remote_api_env_interactive

from logging.config import dictConfig
import argparse
import logging
import os
import sys

import requests
import uvicorn


def _build_log_config() -> dict:
    config = uvicorn.config.LOGGING_CONFIG.copy()
    fmt = "[%(asctime)s] [%(name)s] [%(levelname)s]: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    try:
        config["formatters"]["default"]["fmt"] = fmt
        config["formatters"]["default"]["datefmt"] = datefmt
    except Exception:
        pass
    config = {
        **config,
        "loggers": {
            **config.get("loggers", {}),
            "": {
                "handlers": ["default"],
                "level": "INFO",
            },
            "src": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "src.watchdog": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "src.idle": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
    return config


def run_server() -> int:
    log_config = _build_log_config()
    dictConfig(log_config)
    ensure_guest_credentials_interactive()
    from src.api import create_app
    app = create_app()
    listen_host = os.getenv("REMOTE_VM_API_LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.getenv("REMOTE_VM_API_PORT", "495"))
    uvicorn.run(app, host=listen_host, port=listen_port, log_config=log_config, log_level="info")
    return 0


def run_client() -> int:
    ensure_remote_api_env_interactive()
    api_host = os.getenv("REMOTE_VM_API_HOST", "127.0.0.1")
    api_port = os.getenv("REMOTE_VM_API_PORT", "495")
    base_url = f"http://{api_host}:{api_port}"
    client = VMClient(base_url)
    try:
        names = client.get_vm_list()
    except requests.RequestException as exc:
        msg, code = map_requests_error(exc, base_url)
        print(msg)
        return int(code)
    if not names:
        print("사용 가능한 VM이 없습니다.")
        return 1
    selected_vm = VMClient.choose(names)
    client.vm_name = selected_vm
    print(f"선택된 VM: {selected_vm}")

    try:
        snapshots = client.get_snapshot_list()
    except requests.RequestException as exc:
        msg, code = map_requests_error(exc, base_url)
        print(msg)
        return int(code)
    options = ["현재 상태로 바로 연결"] + snapshots
    print("접속 방법을 선택하세요:")
    method = VMClient.choose(options)

    def _fmt(sec: float | None) -> str:
        return f"~{int(sec)}s" if sec and sec > 0 else "N/A"
    vm_running = client.get_vm_state()
    connect_op = "connect_warm" if vm_running else "connect_cold" if vm_running is not None else "connect"
    et_connect = client.get_expected_time(connect_op) or client.get_expected_time("connect")

    if method != options[0]:
        et_revert = client.get_expected_time("revert")
        total_et = (et_revert or 0) + (et_connect or 0)
        print(f"예상 총 시간: {_fmt(total_et)}")
        client.begin_total_progress(total_et)
        try:
            task_id = client.revert_async(method)
        except requests.RequestException as exc:
            msg, code = map_requests_error(exc, base_url)
            print(msg)
            return int(code)
        res = client.poll_task(task_id, et_revert)
        if res.get("status") != "done":
            print(f"복구 실패: {res.get('error')}")
            return 2
        et_connect = client.get_expected_time("connect_warm") or et_connect
    else:
        print(f"예상 총 시간: {_fmt(et_connect)}")
        client.begin_total_progress(et_connect or 0)

    try:
        task_id = client.connect_async()
    except requests.RequestException as exc:
        msg, code = map_requests_error(exc, base_url)
        print(msg)
        return int(code)
    res = client.poll_task(task_id, et_connect)
    if res.get("status") != "done":
        print(f"연결 실패: {res.get('error')}")
        return int(ExitCode.CONNECT_FAILED)
    ip = res.get("ip")
    if not ip:
        print("IP를 확인할 수 없습니다.")
        return 4
    client.launch_rdp(ip)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="remote-vm-runner entrypoint")
    parser.add_argument(
        "mode",
        choices=["server", "client"],
        help="Run server (FastAPI) or client (simple list)",
    )
    args = parser.parse_args()
    if args.mode == "server":
        return run_server()
    return run_client()


if __name__ == "__main__":
    raise SystemExit(main())
