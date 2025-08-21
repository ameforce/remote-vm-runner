from __future__ import annotations
from src.cli import VMClient
from src.errors import ExitCode, map_requests_error

from logging.config import dictConfig
import argparse
import logging
import os
import sys

import requests
import uvicorn
import getpass
import subprocess


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


def _persist_env_vars(vars_to_set: dict[str, str]) -> str:
    """
    Persist variables to Windows environment.
    Tries system-level first (/M), falls back to user-level on failure.
    Returns scope: "machine" or "user".
    """
    if os.name != "nt":
        for k, v in vars_to_set.items():
            os.environ[k] = v
        return "process"
    scope = "machine"
    try:
        for k, v in vars_to_set.items():
            subprocess.run(["setx", "/M", k, v], check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        scope = "user"
        for k, v in vars_to_set.items():
            try:
                subprocess.run(["setx", k, v], check=True, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    for k, v in vars_to_set.items():
        os.environ[k] = v
    return scope


def _ensure_guest_credentials_interactive() -> None:
    user = (os.getenv("GUEST_USER") or "").strip()
    pw = (os.getenv("GUEST_PASS") or "").strip()
    if user and pw:
        logging.getLogger("main").info("Guest credentials loaded from environment (GUEST_USER=%s).", user)
        return
    if sys.stdin and sys.stdin.isatty():
        print("서버 시작에 필요한 게스트 계정 정보를 설정합니다. (값은 시스템 환경변수에 저장됩니다)")
        while True:
            entered_user = input("게스트 사용자명 (예: administrator): ").strip()
            if entered_user:
                break
            print("사용자명을 입력해주세요.")
        while True:
            entered_pass = getpass.getpass("게스트 비밀번호: ").strip()
            if entered_pass:
                break
            print("비밀번호를 입력해주세요.")
        scope = _persist_env_vars({"GUEST_USER": entered_user, "GUEST_PASS": entered_pass})
        logging.getLogger("main").info("게스트 자격 증명을 %s 환경변수에 저장했습니다. (GUEST_USER=%s)", scope, entered_user)
        return
    logging.getLogger("main").warning("GUEST_USER/GUEST_PASS 미설정이며, TTY가 없어 인터랙티브 입력이 불가합니다.")


def run_server() -> int:
    log_config = _build_log_config()
    dictConfig(log_config)
    _ensure_guest_credentials_interactive()
    from src.api import create_app
    app = create_app()
    listen_host = os.getenv("REMOTE_VM_API_LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.getenv("REMOTE_VM_API_PORT", "495"))
    uvicorn.run(app, host=listen_host, port=listen_port, log_config=log_config, log_level="info")
    return 0


def run_client() -> int:
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
