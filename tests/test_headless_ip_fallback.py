import sys
from pathlib import Path as _Path
import types

ROOT = _Path(__file__).parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import src.vmware as vmw


def test_headless_dhcp_parse_and_arp(monkeypatch, tmp_path):
    monkeypatch.setattr(vmw, "_is_headless", lambda: True)

    mac = "00:50:56:ab:cd:ef"
    vmx_path = tmp_path / "X.vmx"
    vmx_path.write_text(
        """
ethernet0.present = "TRUE"
ethernet0.address = "00:50:56:ab:cd:ef"
""".strip()
    )

    leases = tmp_path / "vmnetdhcp.leases"
    leases.write_text(
        """
lease 192.168.88.123 {
  starts 3 2025/01/01 00:00:00;
  ends 3 2025/01/01 12:00:00;
  hardware ethernet 00:50:56:ab:cd:ef;
}
""".strip()
    )

    monkeypatch.setattr(vmw, "_dhcp_candidate_paths", lambda: [leases])

    monkeypatch.setattr(vmw, "_arp_lookup_ip", lambda m: "")

    ip = vmw.fast_wait_for_ip(vmx_path, timeout=2, probe_interval=0.05)
    assert ip == "192.168.88.123"
