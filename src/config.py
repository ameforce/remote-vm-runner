from __future__ import annotations

import ipaddress
import os
from pathlib import Path


VMRUN: Path = Path(os.getenv("VMRUN_PATH", r"C:\\Program Files (x86)\\VMware\\VMware Workstation\\vmrun.exe"))
VM_ROOT: Path = Path(os.getenv("VM_ROOT", r"C:\\VMware"))
RDP_TEMPLATE_PATH: Path = Path(
    os.getenv("RDP_TEMPLATE_PATH", "") or (Path(__file__).parents[1] / "templates" / "rdp_template.rdp")
)
RDP_CMD: str = os.getenv("RDP_CMD", "mstsc")
ENABLE_CMDKEY_PRELOAD: bool = os.getenv("ENABLE_CMDKEY_PRELOAD", "true").strip().lower() in {"1", "true", "yes"}

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

def _get_env_with_flag(name: str, default: str = "") -> tuple[str, bool]:
    val = os.getenv(name)
    if val is None:
        return default, False
    return val, True

GUEST_USER, GUEST_USER_FROM_ENV = _get_env_with_flag("GUEST_USER", "")
GUEST_PASS, GUEST_PASS_FROM_ENV = _get_env_with_flag("GUEST_PASS", "")

IP_POLL_INTERVAL: float = float(os.getenv("IP_POLL_INTERVAL", "0.2"))
IP_POLL_TIMEOUT: int = int(os.getenv("IP_POLL_TIMEOUT", "120"))

_pref_env = os.getenv("PREFERRED_SUBNETS", "192.168.0.0/22")
PREFERRED_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _pref_env.split(',') if net.strip()]
_ex_env = os.getenv("EXCLUDE_SUBNETS", "")
EXCLUDE_SUBNETS = [ipaddress.ip_network(net.strip()) for net in _ex_env.split(',') if net.strip()]

ENABLE_IDLE_WATCHDOG: bool = True
IDLE_CHECK_INTERVAL_SEC: int = int(os.getenv("IDLE_CHECK_INTERVAL_SEC", "20"))
IDLE_SHUTDOWN_MINUTES: int = int(os.getenv("IDLE_SHUTDOWN_MINUTES", "30"))
IDLE_SHUTDOWN_SECONDS: int = max(30, IDLE_SHUTDOWN_MINUTES * 60)
IDLE_SHUTDOWN_MODE: str = os.getenv("IDLE_SHUTDOWN_MODE", "soft")
IDLE_ONLY_ON_PRESSURE: bool = os.getenv("IDLE_ONLY_ON_PRESSURE", "true").strip().lower() in {"1", "true", "yes"}
RDP_PORT: int = int(os.getenv("RDP_PORT", "3389"))

ASSUME_ACTIVE_ON_FAILURE: bool = os.getenv("ASSUME_ACTIVE_ON_FAILURE", "false").strip().lower() in {"1", "true", "yes"}

RDP_PS_TIMEOUT_SEC: int = int(os.getenv("RDP_PS_TIMEOUT_SEC", "10"))
RDP_QUSER_TIMEOUT_SEC: int = int(os.getenv("RDP_QUSER_TIMEOUT_SEC", "6"))

ASSUME_ACTIVE_IF_RDP_LISTENING: bool = os.getenv("ASSUME_ACTIVE_IF_RDP_LISTENING", "true").strip().lower() in {"1", "true", "yes"}
TCP_PROBE_TIMEOUT_SEC: float = float(os.getenv("TCP_PROBE_TIMEOUT_SEC", "1.0"))

RDP_CHECK_BUDGET_SEC: float = float(os.getenv("RDP_CHECK_BUDGET_SEC", "1.0"))

ENABLE_TOOLS_SELF_HEAL: bool = os.getenv("ENABLE_TOOLS_SELF_HEAL", "true").strip().lower() in {"1", "true", "yes"}
TOOLS_RESTART_COOLDOWN_SEC: int = int(os.getenv("TOOLS_RESTART_COOLDOWN_SEC", "600"))

MIN_AVAILABLE_MEM_GB: float = float(os.getenv("MIN_AVAILABLE_MEM_GB", "4"))
MAX_SHUTDOWNS_PER_TICK: int = int(os.getenv("MAX_SHUTDOWNS_PER_TICK", "2"))
CPU_PRESSURE_THRESHOLD_PCT: int = int(os.getenv("CPU_PRESSURE_THRESHOLD_PCT", "85"))
CPU_SAMPLE_DURATION_SEC: float = float(os.getenv("CPU_SAMPLE_DURATION_SEC", "1.0"))
CPU_CONSECUTIVE_TICKS: int = int(os.getenv("CPU_CONSECUTIVE_TICKS", "3"))

_RDP_DETECTION_MODE_RAW = os.getenv("RDP_DETECTION_MODE", "tcp").strip().lower()
_ALLOWED_RDP_MODES = {"fast", "hybrid", "thorough", "tcp", "off"}
RDP_DETECTION_MODE: str = _RDP_DETECTION_MODE_RAW if _RDP_DETECTION_MODE_RAW in _ALLOWED_RDP_MODES else "hybrid"

RDP_CHECK_CONCURRENCY: int = max(1, int(os.getenv("RDP_CHECK_CONCURRENCY", "2")))
RDP_CHECK_BATCH_SIZE: int = max(0, int(os.getenv("RDP_CHECK_BATCH_SIZE", "0")))

REQUIRE_GUEST_CREDENTIALS: bool = os.getenv("REQUIRE_GUEST_CREDENTIALS", "false").strip().lower() in {"1", "true", "yes"}

SKIP_TOOLS_WAIT_WHEN_HEADLESS: bool = os.getenv("SKIP_TOOLS_WAIT_WHEN_HEADLESS", "true").strip().lower() in {"1", "true", "yes"}
ENABLE_HEADLESS_IP_FALLBACK: bool = os.getenv("ENABLE_HEADLESS_IP_FALLBACK", "true").strip().lower() in {"1", "true", "yes"}

DHCP_LEASES_PATHS_RAW: str = os.getenv("DHCP_LEASES_PATHS", "").strip()

RDP_READY_WAIT_SEC: float = float(os.getenv("RDP_READY_WAIT_SEC", "45"))
RDP_READY_PROBE_INTERVAL_SEC: float = float(os.getenv("RDP_READY_PROBE_INTERVAL_SEC", "0.5"))

RDP_STATUS_CACHE_TTL_SEC: int = int(os.getenv("RDP_STATUS_CACHE_TTL_SEC", "30"))
RDP_CLIENTS_CACHE_TTL_SEC: int = int(os.getenv("RDP_CLIENTS_CACHE_TTL_SEC", "30"))
RDP_ACTIVE_SCAN_MAX_WORKERS: int = max(1, int(os.getenv("RDP_ACTIVE_SCAN_MAX_WORKERS", "4")))
RDP_CLIENTS_SCAN_MAX_WORKERS: int = max(1, int(os.getenv("RDP_CLIENTS_SCAN_MAX_WORKERS", "2")))
RDP_HINT_MAX_WORKERS: int = max(1, int(os.getenv("RDP_HINT_MAX_WORKERS", "8")))

RDP_MONITOR_ENABLED: bool = os.getenv("RDP_MONITOR_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
RDP_MONITOR_INTERVAL_SEC: int = int(os.getenv("RDP_MONITOR_INTERVAL_SEC", "20"))
RDP_MONITOR_MAX_WORKERS: int = max(1, int(os.getenv("RDP_MONITOR_MAX_WORKERS", "2")))
