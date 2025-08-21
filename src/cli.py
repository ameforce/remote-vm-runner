from __future__ import annotations

from typing import List
import shutil
import subprocess
import sys
import tempfile
import time
import os

import requests
import tqdm
from .config import RDP_TEMPLATE_PATH, RDP_CMD, ENABLE_CMDKEY_PRELOAD


class VMClient:
    def __init__(self, api_base: str, default_vm: str = "init", rdp_cmd: str = RDP_CMD) -> None:
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
        use_bar = isinstance(expected, (int, float)) and (expected or 0) > 0
        pbar = None
        last_shown = 0.0
        try:
            if use_bar:
                gp = getattr(self, "_global_progress", None)
                total_secs = int(round(float(gp.get("expected", expected)))) if isinstance(gp, dict) else int(round(float(expected)))
                pbar = tqdm.tqdm(
                    total=total_secs,
                    unit="s",
                    dynamic_ncols=True,
                    bar_format="{bar} {percentage:3.0f}% | {desc}",
                    leave=False,
                    ascii=False,
                )
                pbar.set_description_str(f"0/{total_secs}s | 남은 {total_secs}s | 대기 중")

            while True:
                r = requests.get(f"{self.api_base}/task/{task_id}", timeout=5).json()
                status = r["status"]
                progress = r.get("progress", "")
                elapsed = time.perf_counter() - start

                if use_bar and pbar is not None:
                    status_msg = self._build_status_message(status, progress)
                    gp = getattr(self, "_global_progress", None)
                    if isinstance(gp, dict) and float(gp.get("expected", 0)) > 0 and float(gp.get("start", 0)) > 0:
                        global_elapsed = int(time.perf_counter() - float(gp["start"]))
                        display_total = int(round(float(gp["expected"])))
                        remaining = max(display_total - global_elapsed, 0)
                        pbar.set_description_str(f"{global_elapsed}/{display_total}s | 남은 {remaining}s | {status_msg}")
                        target_n = min(global_elapsed, int(pbar.total or global_elapsed))
                        delta = target_n - int(pbar.n)
                        if delta > 0:
                            pbar.update(delta)
                    else:
                        local_elapsed = int(elapsed)
                        display_total = int(pbar.total or 0)
                        remaining = max(display_total - local_elapsed, 0)
                        pbar.set_description_str(f"{local_elapsed}/{display_total}s | 남은 {remaining}s | {status_msg}")
                        inc = int(elapsed) - int(last_shown)
                        if inc > 0:
                            remaining_allowed = max((pbar.total or 0) - int(pbar.n), 0)
                            apply_inc = min(inc, remaining_allowed)
                            if apply_inc > 0:
                                pbar.update(apply_inc)
                else:
                    total_eta = self._compute_total_eta()
                    line = self._render_progress(elapsed, expected, status, progress, total_eta=total_eta)
                    cols = shutil.get_terminal_size(fallback=(120, 20)).columns
                    sys.stdout.write("\r" + " " * (cols - 1) + "\r")
                    sys.stdout.write(line[: cols - 1])
                    sys.stdout.flush()

                if status in ("done", "failed"):
                    if use_bar and pbar is not None:
                        remaining = (pbar.total or 0) - int(pbar.n)
                        if remaining > 0:
                            pbar.update(remaining)
                        pbar.close()
                        print()
                    else:
                        print()
                    return r
                last_shown = elapsed
                time.sleep(0.2)
        finally:
            if pbar is not None:
                try:
                    pbar.close()
                except Exception:
                    pass

    @staticmethod
    def _render_progress(elapsed: float, expected: float | None, status: str, progress_msg: str, total_eta: int | None = None) -> str:
        if expected is None or expected <= 0:
            spinner = "|/-\\"
            idx = int(elapsed) % len(spinner)
            msg = VMClient._build_status_message(status, progress_msg)
            if total_eta is not None:
                return f"{spinner[idx]} elapsed {int(elapsed)}s | 총 남은 시간 {total_eta}s | {msg}"
            return f"{spinner[idx]} elapsed {int(elapsed)}s | {msg}"
        ratio = min(elapsed / expected, 1.0)
        bar_len = 30
        filled = int(bar_len * ratio)
        bar = "█" * filled + " " * (bar_len - filled)
        remaining = max(int(expected - elapsed), 0)
        msg = VMClient._build_status_message(status, progress_msg)
        if total_eta is not None:
            return f"[{bar}] {int(ratio*100):3d}% | elapsed {int(elapsed)}s | 총 남은 시간 {total_eta}s | {msg}"
        return f"[{bar}] {int(ratio*100):3d}% | {int(elapsed)}/{int(expected)}s | 남은 {remaining}s | {msg}"

    @staticmethod
    def _build_status_message(status: str, progress_msg: str, max_len: int = 50) -> str:
        msg = status or ""
        if progress_msg:
            msg = f"{msg} – {progress_msg}" if msg else progress_msg
        if len(msg) > max_len:
            return msg[: max_len - 1] + "…"
        return msg

    def _compute_total_eta(self) -> int | None:
        gp = getattr(self, "_global_progress", None)
        if not isinstance(gp, dict):
            return None
        try:
            expected_total = float(gp.get("expected", 0))
            start_ts = float(gp.get("start", 0))
            if expected_total <= 0 or start_ts <= 0:
                return None
            elapsed = time.perf_counter() - start_ts
            remaining = max(int(round(expected_total)) - int(elapsed), 0)
            return remaining
        except Exception:
            return None

    def begin_total_progress(self, expected_total: float | None) -> None:
        if isinstance(expected_total, (int, float)) and (expected_total or 0) > 0:
            self._global_progress = {"start": time.perf_counter(), "expected": float(expected_total)}
        else:
            self._global_progress = None

    def get_vm_state(self) -> bool | None:
        try:
            resp = requests.get(f"{self.api_base}/vm_state", params={"vm": self.vm_name}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return bool(data.get("running"))
        except requests.RequestException:
            return None

    @staticmethod
    def choose(items: List[str]) -> str:
        for idx, item in enumerate(items, 1):
            print(f"[{idx}] {item}")
        prompt = "번호 선택(Enter=1) ▶ "
        while True:
            sel_str = input(prompt).strip()
            if sel_str == "":
                print()
                return items[0]
            if sel_str.isdigit():
                sel = int(sel_str) - 1
                if 0 <= sel < len(items):
                    print()
                    return items[sel]
            print("잘못된 입력, 다시 시도하세요.")

    @staticmethod
    def create_rdp_file(ip: str, username: str) -> str:
        if RDP_TEMPLATE_PATH.exists():
            template = RDP_TEMPLATE_PATH.read_text(encoding="utf-8", errors="ignore")
            rdp_content = template.replace("{ip}", ip).replace("{username}", username)
            if "full address:s:" not in rdp_content:
                rdp_content += f"\nfull address:s:{ip}"
            if "username:s:" not in rdp_content:
                rdp_content += f"\nusername:s:{username}"
        else:
            rdp_content = f"full address:s:{ip}\nusername:s:{username}\nauthentication level:i:0\nprompt for credentials:i:0\npromptcredentialonce:i:1\nnegotiate security layer:i:1\nenablecredsspsupport:i:1\n"
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".rdp", delete=False)
        tmp.write(rdp_content)
        tmp.close()
        return tmp.name

    @staticmethod
    def preload_rdp_credentials(ip: str, username: str, password: str) -> None:
        try:
            if os.name != "nt":
                return
            cmdkey_path = shutil.which("cmdkey")
            if not cmdkey_path:
                return
            args = [
                cmdkey_path,
                f"/generic:TERMSRV/{ip}",
                f"/user:{username}",
                f"/pass:{password}",
            ]
            subprocess.run(args, check=False, shell=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def launch_rdp(self, ip: str) -> None:
        print(f"원격 데스크톱 연결 시작: {ip}")
        print()
        try:
            cred = requests.get(f"{self.api_base}/guest_credentials", timeout=5).json()
            guest_user = (cred.get("guest_user") or "").strip()
            guest_pass = (cred.get("guest_pass") or "").strip()
        except Exception:
            guest_user = ""
            guest_pass = ""

        if guest_user and guest_pass:
            print("서버 제공 자격 증명으로 자동 로그인을 시도합니다.")
        else:
            print("서버에 저장된 계정/비밀번호가 없어 자동 로그인을 진행할 수 없습니다.")
        print()

        if ENABLE_CMDKEY_PRELOAD and guest_user and guest_pass:
            try:
                self.preload_rdp_credentials(ip, guest_user, guest_pass)
            except Exception:
                pass

        try:
            rdp_file = self.create_rdp_file(ip, username=guest_user)
            try:
                title = (self.vm_name or ip).strip()
                title = "".join((c if c not in '<>:"/\\|?*' and ord(c) >= 32 else "_") for c in title).rstrip(" .")
                if not title:
                    title = "connection"
                target_path = os.path.join(tempfile.gettempdir(), f"{title}.rdp")
                try:
                    os.replace(rdp_file, target_path)
                    launch_path = target_path
                except Exception:
                    launch_path = rdp_file
            except Exception:
                launch_path = rdp_file
            subprocess.Popen([self.rdp_cmd, launch_path], shell=False)
            print("RDP 클라이언트가 시작되었습니다.")
        except Exception as exc:
            print(f"RDP 연결 실패: {exc}")
            print("수동으로 RDP 연결을 시도해주세요:")
            print(f"   명령어: mstsc /v:{ip}")
            if not guest_user or not guest_pass:
                print("   서버 측에 저장된 계정/비밀번호가 없어 자동 로그인을 수행할 수 없습니다.")
