from __future__ import annotations
from src.cli import VMClient
import src.cli as cli
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
    try:
        if "handlers" in config:
            if "default" in config["handlers"]:
                config["handlers"]["default"]["level"] = "DEBUG"
            if "access" in config["handlers"]:
                config["handlers"]["access"]["level"] = "DEBUG"
    except Exception:
        pass
    config = {
        **config,
        "loggers": {
            **config.get("loggers", {}),
            "": {
                "handlers": ["default"],
                "level": "DEBUG",
            },
            "uvicorn": {
                "handlers": ["default"],
                "level": "DEBUG",
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": ["default"],
                "level": "DEBUG",
                "propagate": False,
            },
            "src": {
                "handlers": ["default"],
                "level": "DEBUG",
                "propagate": False,
            },
            "src.network": {
                "handlers": ["default"],
                "level": "DEBUG",
                "propagate": False,
            },
            "src.watchdog": {
                "handlers": ["default"],
                "level": "DEBUG",
                "propagate": False,
            },
            "src.idle": {
                "handlers": ["default"],
                "level": "DEBUG",
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
    uvicorn.run(app, host=listen_host, port=listen_port, log_config=log_config, log_level="debug")
    return 0


def run_client() -> int:
    log = logging.getLogger("main.client")
    ensure_remote_api_env_interactive()
    api_host = os.getenv("REMOTE_VM_API_HOST", "127.0.0.1").strip()
    api_port = os.getenv("REMOTE_VM_API_PORT", "495").strip()
    base_url = f"http://{api_host}:{api_port}"
    log.info("Using API base: %s", base_url)
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
    print("VM을 선택하세요:")
    for idx, name in enumerate(names, 1):
        print(f"[{idx}] {name}  (사용자: 확인 중)")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import shutil as _shutil

    total = len(names)
    labels: list[str] = [f"{nm}  (사용자: 확인 중)" for nm in names]
    active_flags: dict[str, bool | None] = {nm: None for nm in names}
    RED = "\x1b[31m"
    GREEN = "\x1b[32m"
    RESET = "\x1b[0m"

    def _render_all() -> None:
        try:
            if total > 0:
                sys.stdout.write(f"\x1b[{total}A")
            for i, text in enumerate(labels, 1):
                cols = _shutil.get_terminal_size(fallback=(120, 20)).columns
                try:
                    vm_name = names[i - 1]
                    flag = active_flags.get(vm_name)
                    if flag is True:
                        prefix, suffix = RED, RESET
                    elif flag is False:
                        prefix, suffix = GREEN, RESET
                    else:
                        prefix, suffix = "", ""
                except Exception:
                    prefix, suffix = "", ""
                line = f"{prefix}[{i}] {text}{suffix}"
                sys.stdout.write("\r\x1b[2K")
                sys.stdout.write(line[: max(cols - 1, 1)])
                sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max(1, min(8, total))) as pool:
        future_to_idx = {pool.submit(client.get_rdp_used, nm): (i, nm) for i, nm in enumerate(names, 1)}
        for fut in as_completed(future_to_idx):
            i, nm = future_to_idx[fut]
            try:
                active, clients = fut.result()
            except Exception:
                labels[i - 1] = f"{nm}  (사용자: 확인 에러 발생)"
                active_flags[nm] = None
                _render_all()
                continue
            log.debug("rdp_used result: vm=%s active=%s clients=%s", nm, active, clients)
            active_flags[nm] = bool(active)
            if active_flags[nm]:
                labels[i - 1] = f"{nm}  (사용자: 있음)"
            else:
                labels[i - 1] = f"{nm}  (사용자: 없음)"
            _render_all()

    print()
    cli.SUPPRESS_LIST_PRINT_ONCE = True
    selected_vm = VMClient.choose(names)
    client.vm_name = selected_vm
    print(f"선택된 VM: {selected_vm}")

    try:
        snapshots = client.get_snapshot_list()
    except requests.RequestException as exc:
        msg, code = map_requests_error(exc, base_url)
        print(msg)
        return int(code)

    is_selected_active = bool(active_flags.get(selected_vm))
    options_raw = ["현재 상태로 바로 연결"] + snapshots
    if is_selected_active:
        display_options = [f"{GREEN}{options_raw[0]}{RESET}"] + [f"{RED}{snap} (선택 불가){RESET}" for snap in snapshots]
    else:
        display_options = list(options_raw)
    print("접속 방법을 선택하세요:")
    while True:
        choice_display = VMClient.choose(display_options)
        try:
            idx = display_options.index(choice_display)
        except ValueError:
            idx = 0
        method = options_raw[idx]
        if is_selected_active and idx != 0:
            print("현재 VM에 활성 사용자 세션이 있어 스냅샷 복구를 선택할 수 없습니다.")
            continue
        break

    def _fmt(sec: float | None) -> str:
        return f"~{int(sec)}s" if sec and sec > 0 else "N/A"
    vm_running = client.get_vm_state()
    connect_op = "connect_warm" if vm_running else "connect_cold" if vm_running is not None else "connect"
    et_connect = client.get_expected_time(connect_op) or client.get_expected_time("connect")

    if method != options_raw[0]:
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
