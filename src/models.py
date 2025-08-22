from __future__ import annotations

from pydantic import BaseModel, Field
from typing import List


class SnapshotListResponse(BaseModel):
    vm: str
    snapshots: List[str] = Field(..., description="Snapshot names")


class RevertRequest(BaseModel):
    snapshot: str
    vm: str = "init"


class RevertResponse(BaseModel):
    vm: str
    snapshot: str
    ip: str


class ConnectRequest(BaseModel):
    vm: str = "init"


class ConnectResponse(BaseModel):
    vm: str
    ip: str


class ExpectedTimeResponse(BaseModel):
    vm: str
    op: str
    avg_seconds: float | None


class TaskInfo(BaseModel):
    status: str
    progress: str = "대기 중"
    ip: str | None = None
    started: float | None = None
    finished: float | None = None
    error: str | None = None


class VMListItem(BaseModel):
    name: str
    vmx: str
    clients: List[str] = Field(default_factory=list)
    active: bool = Field(default=False)


class VMListResponse(BaseModel):
    root: str
    vms: List[VMListItem]


class IdlePolicy(BaseModel):
    enabled: bool = Field(default=False, description="Enable idle shutdown watchdog")
    idle_minutes: int = Field(default=5, description="Minutes of no RDP activity before shutdown (0 = immediate)")
    check_interval_sec: int = Field(default=60, description="Watchdog tick interval seconds")
    mode: str = Field(default="soft", description="Shutdown mode: soft|hard")
    only_on_pressure: bool = Field(
        default=False,
        description="When true, perform idle shutdown only if host is under resource pressure",
    )


class ResourcePolicy(BaseModel):
    min_available_mem_gb: float = Field(default=6.0, description="When host available memory (GB) falls below this, reclaim idle VMs")
    max_shutdowns_per_tick: int = Field(default=2, description="Max number of VMs to stop in a single sweep")
    cpu_pressure_threshold_pct: int = Field(default=95, description="When CPU usage exceeds this percent, treat as pressure")
    cpu_consecutive_ticks: int = Field(default=3, description="Consecutive ticks above threshold required to trigger CPU pressure")


class IdleState(BaseModel):
    vm: str
    vmx: str
    last_active_ts: float | None = None
    shutting_down: bool = False
