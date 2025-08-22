from __future__ import annotations

import sys
from pathlib import Path as _Path

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import src.envutils as env


def test_compute_new_path_and_contains_idempotent(monkeypatch):
    base = r"C:\\Windows\\System32"
    add = r"C:\\Workspace\\Git\\Tools\\remote-vm-runner"
    p1 = env._compute_new_path(base, add)
    assert p1.startswith(add)
    assert env._path_contains(p1, add)
    p2 = env._compute_new_path(p1, add)
    assert p2 == p1


def test_ensure_guest_credentials_interactive_flow(monkeypatch):
    # Clear env
    monkeypatch.delenv("GUEST_USER", raising=False)
    monkeypatch.delenv("GUEST_PASS", raising=False)

    inputs = {"user": ["", "administrator"], "pass": ["", "secret!"]}

    def fake_isatty():
        return True

    def fake_input(prompt: str) -> str:
        return inputs["user"].pop(0)

    def fake_getpass(prompt: str) -> str:
        return inputs["pass"].pop(0)

    result = env.ensure_guest_credentials_interactive(
        persist_to_system=False,
        isatty_fn=fake_isatty,
        input_fn=fake_input,
        getpass_fn=fake_getpass,
    )

    assert result == "ok_persisted"
    assert env.os.environ.get("GUEST_USER") == "administrator"
    assert env.os.environ.get("GUEST_PASS") == "secret!"


def test_ensure_remote_api_env_interactive(monkeypatch):
    monkeypatch.delenv("REMOTE_VM_API_HOST", raising=False)
    monkeypatch.delenv("REMOTE_VM_API_PORT", raising=False)

    answers = {"host": ["", "127.0.0.1"], "port": ["", "abc", "495"]}

    def fake_isatty():
        return True

    def fake_input(prompt: str) -> str:
        if "호스트" in prompt:
            return answers["host"].pop(0)
        return answers["port"].pop(0)

    res = env.ensure_remote_api_env_interactive(
        persist_to_system=False, isatty_fn=fake_isatty, input_fn=fake_input
    )
    assert res == "ok_persisted"
    assert env.os.environ.get("REMOTE_VM_API_HOST") == "127.0.0.1"
    assert env.os.environ.get("REMOTE_VM_API_PORT") == "495"


