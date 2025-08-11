from __future__ import annotations
from typing import List

import os
import sys
import time
import requests
import tempfile
import threading
import subprocess
import shutil
import logging

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] [%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')

logger = logging.getLogger("vm-cli")

API_BASE = "http://192.168.0.6:495"
VM_NAME = "init"
RDP_CMD = r"mstsc"


def get_expected_time(op: str) -> float | None:
    url = f"{API_BASE}/expected_time"
    try:
        resp = requests.get(url, params={"vm": VM_NAME, "op": op}, timeout=5)
        resp.raise_for_status()
        return resp.json().get("avg_seconds")
    except requests.RequestException:
        return None


def get_snapshot_list() -> List[str]:
    url = f"{API_BASE}/snapshots"
    resp = requests.get(url, params={"vm": VM_NAME}, timeout=10)
    if resp.status_code != 200:
        logger.warning("서버 응답: %s", resp.status_code)
        logger.warning("응답 내용: %s", resp.text)
    resp.raise_for_status()
    return resp.json()["snapshots"]


def get_vm_list() -> List[str]:
    url = f"{API_BASE}/vms"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        logger.warning("서버 응답: %s", resp.status_code)
        logger.warning("응답 내용: %s", resp.text)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("vms", [])
    names = [item.get("name") for item in items if isinstance(item, dict) and item.get("name")]
    names.sort()
    return names


def revert_to_snapshot(snapshot: str) -> str:
    url = f"{API_BASE}/revert"
    resp = requests.post(url, json={"vm": VM_NAME, "snapshot": snapshot}, timeout=300)
    resp.raise_for_status()
    return resp.json()["ip"]


def connect_async() -> str:
    resp = requests.post(f"{API_BASE}/connect_async", json={"vm": VM_NAME}, timeout=10)
    resp.raise_for_status()
    return resp.json()["task_id"]


def wait_for_vm_ready(ip: str, timeout: int = 60, interval: float = 1.0) -> bool:
    """ping 로 VM 준비 여부 확인"""
    print(f"VM 준비 상태 확인: {ip} (최대 {timeout}초, 주기 {interval}s)")
    start = time.perf_counter()
    spinner = "|/-\\"
    spin_idx = 0

    while True:
        elapsed = int(time.perf_counter() - start)
        if elapsed >= timeout:
            print(f"\nPING 응답 없음 ({timeout}s) – RDP 연결 시도 가능")
            return False
        try:
            result = subprocess.run(["ping", "-n", "1", "-w", "800", ip],
                                    capture_output=True, text=True, timeout=1.5)
            if "TTL=" in result.stdout:
                print(f"\nVM 준비 완료! ({elapsed}s 소요)")
                return True
        except Exception:
            pass
        sys.stdout.write(f"\r   대기 중 {spinner[spin_idx]} {elapsed}s")
        sys.stdout.flush()
        spin_idx = (spin_idx + 1) % len(spinner)
        time.sleep(interval)


def choose(items: List[str]) -> str:
    for idx, item in enumerate(items, 1):
        print(f"[{idx}] {item}")
    prompt = "번호 선택(Enter=1) ▶ "
    while True:
        sel_str = input(prompt).strip()
        if sel_str == "":
            return items[0]
        if sel_str.isdigit():
            sel = int(sel_str) - 1
            if 0 <= sel < len(items):
                return items[sel]
        print("잘못된 입력, 다시 시도하세요.")


def choose_vm() -> str:
    global VM_NAME
    names = sorted(get_vm_list())
    if not names:
        raise SystemExit("사용 가능한 VM이 없습니다. 서버의 /vms 응답을 확인하세요.")
    print("=== 사용할 VM 선택 ===")
    selected = choose(names)
    VM_NAME = selected
    print(f"선택된 VM: {VM_NAME}")
    return selected


def create_rdp_file(ip: str, username: str = "administrator") -> str:
    rdp_content = f"""screen mode id:i:2
use multimon:i:0
desktopwidth:i:1920
desktopheight:i:1080
session bpp:i:32
winposstr:s:0,3,0,0,800,600
compression:i:1
keyboardhook:i:2
audiocapturemode:i:0
videoplaybackmode:i:1
connection type:i:7
networkautodetect:i:1
bandwidthautodetect:i:1
displayconnectionbar:i:1
enableworkspacereconnect:i:0
disable wallpaper:i:0
allow font smoothing:i:0
allow desktop composition:i:0
disable full window drag:i:1
disable menu anims:i:1
disable themes:i:0
disable cursor setting:i:0
bitmapcachepersistenable:i:1
full address:s:{ip}
username:s:{username}
domain:s:
alternate shell:s:
shell working directory:s:
authentication level:i:0
negotiate security layer:i:1
enablecredsspsupport:i:0
"""
    temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.rdp', delete=False)
    temp_file.write(rdp_content)
    temp_file.close()
    return temp_file.name


def launch_rdp(ip: str) -> None:
    print(f"원격 데스크톱 연결 시작: {ip}")
    print("계정: administrator")
    print("비밀번호: epapyrus12#$")
    try:
        print("기본 RDP 연결 시도...")
        subprocess.Popen([RDP_CMD, "/v:" + ip], shell=False)
        print("RDP 클라이언트가 시작되었습니다.")
        print("연결 시 인증 메시지가 표시되면 '예'를 선택하세요.")
        return
    except Exception as e:
        print(f"기본 RDP 연결 실패: {e}")
    try:
        print("RDP 파일 생성 방식으로 재시도...")
        rdp_file = create_rdp_file(ip)
        subprocess.Popen([RDP_CMD, rdp_file], shell=False)

        def cleanup_rdp_file():
            time.sleep(10)
            try:
                os.unlink(rdp_file)
            except Exception:
                pass

        threading.Thread(target=cleanup_rdp_file, daemon=True).start()
        print("RDP 클라이언트가 시작되었습니다.")
        print("사용자명이 미리 입력되어 있으며, 비밀번호만 입력하면 됩니다.")
    except Exception as e:
        print(f"RDP 파일 방식도 실패: {e}")
        print("수동으로 RDP 연결을 시도해주세요:")
        print(f"   명령어: mstsc /v:{ip}")
        print("   계정: administrator")
        print("   비밀번호: epapyrus12#$")


def revert_async(snapshot: str) -> str:
    resp = requests.post(f"{API_BASE}/revert_async", json={"vm": VM_NAME, "snapshot": snapshot}, timeout=10)
    resp.raise_for_status()
    return resp.json()["task_id"]


def _render_progress(elapsed: float, expected: float | None, status: str, progress_msg: str) -> str:
    if expected is None or expected <= 0:
        spinner = "|/-\\"
        idx = int(elapsed) % len(spinner)
        return f"{spinner[idx]} {status} – {progress_msg}"
    ratio = min(elapsed / expected, 1.0)
    bar_len = 30
    filled = int(bar_len * ratio)
    bar = "=" * filled + " " * (bar_len - filled)
    return f"[{bar}] {int(ratio*100):3d}% {status} – {progress_msg}"


def poll_task(task_id: str, expected: float | None):
    print()
    start = time.perf_counter()
    while True:
        r = requests.get(f"{API_BASE}/task/{task_id}", timeout=5).json()
        status = r["status"]
        progress = r.get("progress", "")
        elapsed = time.perf_counter() - start
        line = _render_progress(elapsed, expected, status, progress)
        cols = shutil.get_terminal_size(fallback=(120, 20)).columns
        sys.stdout.write("\r" + " " * (cols - 1) + "\r")
        sys.stdout.write(line[: cols - 1])
        sys.stdout.flush()
        if status in ("done", "failed"):
            print()
            return r
        time.sleep(0.2)


def main() -> None:
    # VM 선택 단계
    try:
        choose_vm()
    except requests.RequestException as exc:
        logger.error("VM 목록 조회 실패: %s", exc)
        sys.exit(1)

    print(f"=== {VM_NAME} 스냅샷 목록 가져오는 중 ===")
    try:
        snaps = get_snapshot_list()
    except requests.RequestException as exc:
        logger.error("API 오류: %s", exc)
        if hasattr(exc, 'response') and exc.response is not None:
            logger.error("응답 내용: %s", exc.response.text)
        sys.exit(1)
    if not snaps:
        sys.exit("스냅샷이 없습니다.")
    print()
    options = ["현재 상태로 바로 접속"] + snaps
    target = choose(options)
    if target == "현재 상태로 바로 접속":
        op = "connect"
        task_id = connect_async()
    else:
        op = "revert"
        print(f"\n'{target}' 으로 복구 요청 중...")
        task_id = revert_async(target)
    expected_total = get_expected_time(op)
    try:
        task_res = poll_task(task_id, expected_total)
        if task_res["status"] != "done":
            sys.exit(f"복구 실패: {task_res.get('error')}")
        ip_addr = task_res.get("ip")
        print(f"완료, IP = {ip_addr}")
    except requests.RequestException as exc:
        sys.exit(f"복구 실패: {exc}")
    launch_rdp(ip_addr)


if __name__ == "__main__":
    main()
