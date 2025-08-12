from __future__ import annotations

import ipaddress
import os
from pathlib import Path


VMRUN: Path = Path(os.getenv("VMRUN_PATH", r"C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe"))
VM_ROOT: Path = Path(os.getenv("VM_ROOT", r"C:\\VMware"))

def _load_alias_map_from_env() -> dict[str, Path]:
    raw = os.getenv("VM_ALIASES", "").strip()
    if not raw:
        return {}
    pairs: dict[str, Path] = {}
    for part in raw.replace(",", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key or not val:
            continue
        pairs[key] = Path(val)
    return pairs

VM_MAP: dict[str, Path] = _load_alias_map_from_env()

GUEST_USER: str = os.getenv("GUEST_USER", "administrator")
GUEST_PASS: str = os.getenv("GUEST_PASS", "epapyrus12#$")

IP_POLL_INTERVAL: float = float(os.getenv("IP_POLL_INTERVAL", "0.2"))
IP_POLL_TIMEOUT: int = int(os.getenv("IP_POLL_TIMEOUT", "120"))

_pref_env = os.getenv("PREFERRED_SUBNETS", "192.168.0.0/22")
PREFERRED_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _pref_env.split(',') if net.strip()]
_ex_env = os.getenv("EXCLUDE_SUBNETS", "")
EXCLUDE_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _ex_env.split(',') if net.strip()]

ENABLE_IDLE_WATCHDOG: bool = True
IDLE_CHECK_INTERVAL_SEC: int = int(os.getenv("IDLE_CHECK_INTERVAL_SEC", "60"))
IDLE_SHUTDOWN_MINUTES: int = int(os.getenv("IDLE_SHUTDOWN_MINUTES", "30"))
IDLE_SHUTDOWN_SECONDS: int = max(30, IDLE_SHUTDOWN_MINUTES * 60)
IDLE_SHUTDOWN_MODE: str = os.getenv("IDLE_SHUTDOWN_MODE", "soft")
RDP_PORT: int = int(os.getenv("RDP_PORT", "3389"))

MIN_AVAILABLE_MEM_GB: float = float(os.getenv("MIN_AVAILABLE_MEM_GB", "6"))
MAX_SHUTDOWNS_PER_TICK: int = int(os.getenv("MAX_SHUTDOWNS_PER_TICK", "2"))
CPU_PRESSURE_THRESHOLD_PCT: int = int(os.getenv("CPU_PRESSURE_THRESHOLD_PCT", "95"))
CPU_SAMPLE_DURATION_SEC: float = float(os.getenv("CPU_SAMPLE_DURATION_SEC", "0.2"))
CPU_CONSECUTIVE_TICKS: int = int(os.getenv("CPU_CONSECUTIVE_TICKS", "3"))
