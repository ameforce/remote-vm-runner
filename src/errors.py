from __future__ import annotations

import os
import requests
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    NO_VMS = 1
    REVERT_FAILED = 2
    CONNECT_FAILED = 3
    IP_NOT_FOUND = 4

    NETWORK_UNAVAILABLE = 10
    HTTP_ERROR = 11
    REQUEST_ERROR = 12


def _build_env_hint() -> str:
    msg = ""
    if not os.getenv("REMOTE_VM_API_HOST"):
        msg += "\n환경 변수 REMOTE_VM_API_HOST이 설정되어 있지 않아 기본 값으로 시도했으나, 실패하였습니다."
    if not os.getenv("REMOTE_VM_API_PORT"):
        msg += "\n환경 변수 REMOTE_VM_API_PORT이 설정되어 있지 않아 기본 값으로 시도했으나, 실패하였습니다."
    return msg


def map_requests_error(exc, base_url: str) -> tuple[str, ExitCode]:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        hint = _build_env_hint()
        if isinstance(exc, requests.Timeout):
            return (f"API 서버 응답이 지연됩니다. URL={base_url}\n{hint}", ExitCode.NETWORK_UNAVAILABLE)
        return (f"API 서버에 연결할 수 없습니다. URL={base_url}\n{hint}", ExitCode.NETWORK_UNAVAILABLE)

    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if getattr(exc, "response", None) is not None else "?"
        return (f"API 오류 응답: HTTP {status} – URL={base_url}", ExitCode.HTTP_ERROR)

    hint = _build_env_hint()
    return (f"네트워크 오류가 발생했습니다. URL={base_url}{hint}", ExitCode.REQUEST_ERROR)
