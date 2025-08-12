"""
Backward-compatible CLI that delegates to rvmrunner.cli.VMClient but preserves
the original UX and tests.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import requests

# Ensure src layout import
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(ROOT_DIR))
if SRC_DIR.is_dir():
    sys.path.insert(0, str(SRC_DIR))

from cli import VMClient

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger("vm-cli")

API_BASE = "http://192.168.0.6:495"
VM_NAME = "init"

_client_singleton: VMClient | None = None


def _client() -> VMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = VMClient(API_BASE, default_vm=VM_NAME)
    return _client_singleton


# Legacy shims expected by tests
def get_vm_list():
    return _client().get_vm_list()


def choose(items):
    return VMClient.choose(items)


def choose_vm() -> str:
    names = sorted(get_vm_list())
    if not names:
        raise SystemExit("사용 가능한 VM이 없습니다. 서버의 /vms 응답을 확인하세요.")
    print("=== 사용할 VM 선택 ===")
    selected = choose(names)
    global VM_NAME
    VM_NAME = selected
    _client().vm_name = selected
    print(f"선택된 VM: {VM_NAME}")
    return selected


def choose_vm_advanced(client: VMClient) -> str:
    names = sorted(client.get_vm_list())
    if not names:
        raise SystemExit("사용 가능한 VM이 없습니다. 서버의 /vms 응답을 확인하세요.")
    print("=== 사용할 VM 선택 ===")
    selected = client.choose(names)
    client.vm_name = selected
    print(f"선택된 VM: {client.vm_name}")
    return selected


def main() -> None:
    client = VMClient(API_BASE)
    try:
        choose_vm_advanced(client)
    except requests.RequestException as exc:
        logger.error("VM 목록 조회 실패: %s", exc)
        sys.exit(1)

    print(f"=== {client.vm_name} 스냅샷 목록 가져오는 중 ===")
    try:
        snaps = client.get_snapshot_list()
    except requests.RequestException as exc:
        logger.error("API 오류: %s", exc)
        if hasattr(exc, 'response') and exc.response is not None:
            logger.error("응답 내용: %s", exc.response.text)
        sys.exit(1)
    if not snaps:
        sys.exit("스냅샷이 없습니다.")
    print()
    options = ["현재 상태로 바로 접속"] + snaps
    target = client.choose(options)
    if target == "현재 상태로 바로 접속":
        op = "connect"
        task_id = client.connect_async()
    else:
        op = "revert"
        print(f"\n'{target}' 으로 복구 요청 중...")
        task_id = client.revert_async(target)
    expected_total = client.get_expected_time(op)
    try:
        task_res = client.poll_task(task_id, expected_total)
        if task_res["status"] != "done":
            sys.exit(f"복구 실패: {task_res.get('error')}")
        ip_addr = task_res.get("ip")
        print(f"완료, IP = {ip_addr}")
    except requests.RequestException as exc:
        sys.exit(f"복구 실패: {exc}")
    client.launch_rdp(ip_addr)


if __name__ == "__main__":
    main()
