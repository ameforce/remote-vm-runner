from __future__ import annotations
from src.api import create_app
from src.cli import VMClient

import argparse
import uvicorn


def run_server() -> int:
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=495, workers=1)
    return 0


def run_client() -> int:
    client = VMClient("http://192.168.0.6:495")
    names = client.get_vm_list()
    if not names:
        print("사용 가능한 VM이 없습니다.")
        return 1
    selected_vm = VMClient.choose(names)
    client.vm_name = selected_vm
    print(f"선택된 VM: {selected_vm}")

    snapshots = client.get_snapshot_list()
    options = ["현재 상태로 바로 연결"] + snapshots
    print("접속 방법을 선택하세요:")
    method = VMClient.choose(options)

    def _fmt(sec: float | None) -> str:
        return f"~{int(sec)}s" if sec and sec > 0 else "N/A"
    et_connect = client.get_expected_time("connect")

    if method != options[0]:
        et_revert = client.get_expected_time("revert")
        print(f"예상 시간 – 복구: {_fmt(et_revert)}, 연결: {_fmt(et_connect)}")
        task_id = client.revert_async(method)
        res = client.poll_task(task_id, et_revert)
        if res.get("status") != "done":
            print(f"복구 실패: {res.get('error')}")
            return 2
    else:
        print(f"예상 시간 – 연결: {_fmt(et_connect)}")

    task_id = client.connect_async()
    res = client.poll_task(task_id, et_connect)
    if res.get("status") != "done":
        print(f"연결 실패: {res.get('error')}")
        return 3
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
