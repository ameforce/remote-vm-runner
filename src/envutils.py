from __future__ import annotations

import getpass
import logging
import os
import subprocess
from typing import Callable


_LOG = logging.getLogger("src.envutils")


def persist_env_vars(vars_to_set: dict[str, str], prefer_machine: bool = True) -> str:
    if os.name != "nt":
        for key, value in vars_to_set.items():
            os.environ[key] = value
        return "process"

    scope = "machine" if prefer_machine else "user"
    if prefer_machine:
        try:
            for key, value in vars_to_set.items():
                subprocess.run(
                    ["setx", "/M", key, value],
                    check=True,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            scope = "user"
            for key, value in vars_to_set.items():
                try:
                    subprocess.run(
                        ["setx", key, value],
                        check=True,
                        shell=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    pass
    else:
        for key, value in vars_to_set.items():
            try:
                subprocess.run(
                    ["setx", key, value],
                    check=True,
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

    for key, value in vars_to_set.items():
        os.environ[key] = value

    return scope


def ensure_guest_credentials_interactive(
    *,
    persist_to_system: bool = True,
    isatty_fn: Callable[[], bool] | None = None,
    input_fn: Callable[[str], str] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
) -> str:
    user = (os.getenv("GUEST_USER") or "").strip()
    pw = (os.getenv("GUEST_PASS") or "").strip()
    if user and pw:
        _LOG.info("Guest credentials loaded from environment (GUEST_USER=%s).", user)
        return "ok_env"

    if isatty_fn is None:
        def _isatty_default() -> bool:
            return bool(os.isatty(0))
        isatty_fn = _isatty_default
    if input_fn is None:
        input_fn = input
    if getpass_fn is None:
        getpass_fn = getpass.getpass

    if isatty_fn():
        print("서버/클라이언트 실행에 필요한 게스트 계정 정보를 설정합니다. (값은 시스템 환경변수에 저장됩니다)")
        while True:
            entered_user = input_fn("게스트 사용자명 (예: administrator): ").strip()
            if entered_user:
                break
            print("사용자명을 입력해주세요.")
        while True:
            entered_pass = getpass_fn("게스트 비밀번호: ").strip()
            if entered_pass:
                break
            print("비밀번호를 입력해주세요.")

        scope = "process"
        if persist_to_system:
            scope = persist_env_vars({"GUEST_USER": entered_user, "GUEST_PASS": entered_pass})
        else:
            os.environ["GUEST_USER"] = entered_user
            os.environ["GUEST_PASS"] = entered_pass
        _LOG.info("게스트 자격 증명을 %s 환경변수에 저장했습니다. (GUEST_USER=%s)", scope, entered_user)
        return "ok_persisted"

    _LOG.warning("GUEST_USER/GUEST_PASS 미설정이며, TTY가 없어 인터랙티브 입력이 불가합니다.")
    return "no_tty"


def ensure_remote_api_env_interactive(
    *,
    persist_to_system: bool = True,
    isatty_fn: Callable[[], bool] | None = None,
    input_fn: Callable[[str], str] | None = None,
) -> str:
    host = (os.getenv("REMOTE_VM_API_HOST") or "").strip()
    port = (os.getenv("REMOTE_VM_API_PORT") or "").strip()
    if host and port:
        _LOG.info("API endpoint loaded from environment: %s:%s", host, port)
        return "ok_env"

    if isatty_fn is None:
        import sys as _sys
        def _stdin_isatty() -> bool:
            try:
                return bool(getattr(_sys, "stdin", None) and _sys.stdin.isatty())
            except Exception:
                return False
        isatty_fn = _stdin_isatty
    if isatty_fn is None:
        import sys as _sys
        def _stdin_isatty() -> bool:
            try:
                return bool(getattr(_sys, "stdin", None) and _sys.stdin.isatty())
            except Exception:
                return False
        isatty_fn = _stdin_isatty
    if input_fn is None:
        input_fn = input

    if isatty_fn():
        print("API 접속 정보를 설정합니다. (값은 환경변수에 저장됩니다)")

        def _ask_host() -> str:
            while True:
                value = input_fn("API 호스트 (기본=127.0.0.1): ").strip() or "127.0.0.1"
                if value:
                    return value

        def _ask_port() -> str:
            while True:
                value = input_fn("API 포트 (기본=495): ").strip() or "495"
                try:
                    iv = int(value)
                    if 1 <= iv <= 65535:
                        return str(iv)
                except Exception:
                    pass
                print("올바른 포트 번호를 입력해주세요 (1-65535).")

        entered_host = _ask_host()
        entered_port = _ask_port()

        scope = "process"
        if persist_to_system:
            scope = persist_env_vars({"REMOTE_VM_API_HOST": entered_host, "REMOTE_VM_API_PORT": entered_port})
        else:
            os.environ["REMOTE_VM_API_HOST"] = entered_host
            os.environ["REMOTE_VM_API_PORT"] = entered_port
        _LOG.info("API endpoint %s:%s stored to %s PATH env.", entered_host, entered_port, scope)
        return "ok_persisted"

    _LOG.warning("REMOTE_VM_API_HOST/REMOTE_VM_API_PORT 미설정이며, TTY가 없어 인터랙티브 입력이 불가합니다.")
    return "no_tty"

def _normalize_path_entry(entry: str) -> str:
    entry = (entry or "").strip().strip('"').strip()
    try:
        if os.name == "nt":
            from ntpath import normcase, normpath

            return normcase(normpath(entry))
        else:
            from posixpath import normpath

            return normpath(entry)
    except Exception:
        return entry


def _path_contains(path_value: str, target: str) -> bool:
    norm_target = _normalize_path_entry(target)
    for item in (path_value or "").split(os.pathsep):
        if not item:
            continue
        if _normalize_path_entry(item) == norm_target:
            return True
    return False


def _compute_new_path(path_value: str, to_add: str) -> str:
    if not path_value:
        return to_add
    if _path_contains(path_value, to_add):
        return path_value
    return to_add + os.pathsep + path_value


def ensure_path_contains(dir_path: str, *, prefer_machine: bool = True) -> str:
    dir_path = _normalize_path_entry(dir_path)
    if not dir_path:
        return "process" if os.name != "nt" else ("machine" if prefer_machine else "user")

    current = os.environ.get("PATH") or os.environ.get("Path") or ""
    if _path_contains(current, dir_path):
        return "process" if os.name != "nt" else ("machine" if prefer_machine else "user")

    new_value = _compute_new_path(current, dir_path)

    if os.name != "nt":
        os.environ["PATH"] = new_value
        return "process"

    scope = "machine" if prefer_machine else "user"
    try:
        subprocess.run(
            ["setx", "/M", "PATH", new_value],
            check=True,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        scope = "user"
        try:
            subprocess.run(
                ["setx", "PATH", new_value],
                check=True,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            os.environ["PATH"] = new_value
            return "process"

    os.environ["PATH"] = new_value
    return scope


