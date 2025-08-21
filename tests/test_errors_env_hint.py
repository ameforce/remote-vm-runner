from __future__ import annotations

from unittest.mock import Mock

import requests

from src.errors import map_requests_error, ExitCode


def test_env_hint_when_host_missing(monkeypatch):
    monkeypatch.delenv("REMOTE_VM_API_HOST", raising=False)
    monkeypatch.setenv("REMOTE_VM_API_PORT", "495")

    exc = requests.ConnectionError("boom")
    msg, code = map_requests_error(exc, base_url="http://127.0.0.1:495")

    assert code == ExitCode.NETWORK_UNAVAILABLE
    assert "REMOTE_VM_API_HOST" in msg


def test_env_hint_when_port_missing(monkeypatch):
    monkeypatch.setenv("REMOTE_VM_API_HOST", "127.0.0.1")
    monkeypatch.delenv("REMOTE_VM_API_PORT", raising=False)

    exc = requests.Timeout("slow")
    msg, code = map_requests_error(exc, base_url="http://127.0.0.1:495")

    assert code == ExitCode.NETWORK_UNAVAILABLE
    assert "REMOTE_VM_API_PORT" in msg


def test_http_error_has_no_hint(monkeypatch):
    monkeypatch.delenv("REMOTE_VM_API_HOST", raising=False)
    monkeypatch.delenv("REMOTE_VM_API_PORT", raising=False)

    response = Mock()
    response.status_code = 503
    exc = requests.HTTPError(response=response)
    msg, code = map_requests_error(exc, base_url="http://x:1")

    assert code == ExitCode.HTTP_ERROR
    assert "HTTP 503" in msg
    assert "REMOTE_VM_API_HOST" not in msg and "REMOTE_VM_API_PORT" not in msg
