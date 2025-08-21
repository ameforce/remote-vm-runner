import sys
from pathlib import Path as _Path

import src.cli as cli


ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_create_rdp_file_has_expected_flags(tmp_path):
    path = cli.VMClient.create_rdp_file("1.2.3.4", username="administrator")
    text = _Path(path).read_text(encoding="utf-8")
    assert "full address:s:1.2.3.4" in text
    assert "username:s:administrator" in text
    assert "prompt for credentials:i:0" in text
    assert "promptcredentialonce:i:1" in text
    assert "authentication level:i:0" in text
    assert "enablecredsspsupport:i:1" in text


def test_preload_rdp_credentials_invokes_cmdkey(monkeypatch):
    calls = {"args": None}

    monkeypatch.setattr(cli.os, "name", "nt", raising=False)
    monkeypatch.setattr(cli.shutil, "which", lambda exe: "cmdkey" if exe == "cmdkey" else None)

    def fake_run(args, check=False, shell=False, stdout=None, stderr=None):
        calls["args"] = args
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli.VMClient.preload_rdp_credentials("1.2.3.4", "administrator", "s3cr3t!")
    assert calls["args"][0].lower().endswith("cmdkey") or calls["args"][0] == "cmdkey"
    assert "/generic:TERMSRV/1.2.3.4" in calls["args"]
    assert "/user:administrator" in calls["args"]
    assert "/pass:s3cr3t!" in calls["args"]


def test_launch_rdp_uses_rdp_file(monkeypatch, tmp_path):
    client = cli.VMClient("http://x")

    monkeypatch.setattr(cli.VMClient, "preload_rdp_credentials", lambda *a, **k: None)

    created = {"path": None}

    def fake_create(ip, username=None):
        p = tmp_path / "sample.rdp"
        p.write_text(f"full address:s:{ip}")
        created["path"] = str(p)
        return created["path"]

    monkeypatch.setattr(cli.VMClient, "create_rdp_file", staticmethod(fake_create))

    called = {"args": None}

    def fake_popen(args, shell=False):
        called["args"] = args
        class P:
            pass
        return P()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    import requests as _req
    def _stub_get(url, *args, **kwargs):
        if str(url).endswith("/guest_credentials"):
            return type("R", (), {"json": lambda self=None: {"guest_user": "administrator", "guest_pass": "s"}})()
        raise _req.RequestException("stubbed")
    monkeypatch.setattr(cli.requests, "get", _stub_get)
    client.launch_rdp("2.3.4.5")
    assert called["args"][0] == client.rdp_cmd
    from pathlib import Path as __P
    assert __P(called["args"][1]).suffix.lower() == ".rdp"


def test_launch_rdp_respects_disable_cmdkey(monkeypatch, tmp_path):
    client = cli.VMClient("http://x")

    monkeypatch.setattr(cli, "ENABLE_CMDKEY_PRELOAD", False, raising=False)

    called = {"preload": False}
    monkeypatch.setattr(cli.VMClient, "preload_rdp_credentials", lambda *a, **k: called.update({"preload": True}))

    def fake_create(ip, username=None):
        p = tmp_path / "sample.rdp"
        p.write_text(f"full address:s:{ip}")
        return str(p)

    monkeypatch.setattr(cli.VMClient, "create_rdp_file", staticmethod(fake_create))
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: None)

    import requests as _req
    def _stub_get2(url, *args, **kwargs):
        if str(url).endswith("/guest_credentials"):
            return type("R", (), {"json": lambda self=None: {"guest_user": "administrator", "guest_pass": "secret"}})()
        raise _req.RequestException("stubbed")
    monkeypatch.setattr(cli.requests, "get", _stub_get2)
    client.launch_rdp("9.9.9.9")
    assert called["preload"] is False
