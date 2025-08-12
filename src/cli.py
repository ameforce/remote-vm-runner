from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from typing import List

import requests


class VMClient:
    def __init__(self, api_base: str, default_vm: str = "init", rdp_cmd: str = "mstsc") -> None:
        self.api_base = api_base.rstrip('/')
        self.vm_name = default_vm
        self.rdp_cmd = rdp_cmd

    def get_expected_time(self, op: str) -> float | None:
        try:
            resp = requests.get(f"{self.api_base}/expected_time", params={"vm": self.vm_name, "op": op}, timeout=5)
            resp.raise_for_status()
            return resp.json().get("avg_seconds")
        except requests.RequestException:
            return None

    def get_snapshot_list(self) -> List[str]:
        resp = requests.get(f"{self.api_base}/snapshots", params={"vm": self.vm_name}, timeout=10)
        resp.raise_for_status()
        return resp.json().get("snapshots", [])

    def get_vm_list(self) -> List[str]:
        resp = requests.get(f"{self.api_base}/vms", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("vms", [])
        names = [item.get("name") for item in items if isinstance(item, dict) and item.get("name")]
        names.sort()
        return names

    def connect_async(self) -> str:
        resp = requests.post(f"{self.api_base}/connect_async", json={"vm": self.vm_name}, timeout=10)
        resp.raise_for_status()
        return resp.json()["task_id"]

    def revert_async(self, snapshot: str) -> str:
        resp = requests.post(f"{self.api_base}/revert_async", json={"vm": self.vm_name, "snapshot": snapshot}, timeout=10)
        resp.raise_for_status()
        return resp.json()["task_id"]

    def poll_task(self, task_id: str, expected: float | None):
        start = time.perf_counter()
        while True:
            r = requests.get(f"{self.api_base}/task/{task_id}", timeout=5).json()
            status = r["status"]
            progress = r.get("progress", "")
            elapsed = time.perf_counter() - start
            line = self._render_progress(elapsed, expected, status, progress)
            cols = shutil.get_terminal_size(fallback=(120, 20)).columns
            sys.stdout.write("\r" + " " * (cols - 1) + "\r")
            sys.stdout.write(line[: cols - 1])
            sys.stdout.flush()
            if status in ("done", "failed"):
                print()
                return r
            time.sleep(0.2)

    @staticmethod
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

    @staticmethod
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

    @staticmethod
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
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".rdp", delete=False)
        tmp.write(rdp_content)
        tmp.close()
        return tmp.name

    def launch_rdp(self, ip: str) -> None:
        print(f"원격 데스크톱 연결 시작: {ip}")
        print("계정: administrator")
        print("비밀번호: epapyrus12#$")
        try:
            print("기본 RDP 연결 시도...")
            subprocess.Popen([self.rdp_cmd, "/v:" + ip], shell=False)
            print("RDP 클라이언트가 시작되었습니다.")
            print("연결 시 인증 메시지가 표시되면 '예'를 선택하세요.")
            return
        except Exception as exc:
            print(f"기본 RDP 연결 실패: {exc}")
        try:
            print("RDP 파일 생성 방식으로 재시도...")
            rdp_file = self.create_rdp_file(ip)
            subprocess.Popen([self.rdp_cmd, rdp_file], shell=False)
        except Exception as exc:
            print(f"RDP 파일 방식도 실패: {exc}")
            print("수동으로 RDP 연결을 시도해주세요:")
            print(f"   명령어: mstsc /v:{ip}")
            print("   계정: administrator")
            print("   비밀번호: epapyrus12#$")
