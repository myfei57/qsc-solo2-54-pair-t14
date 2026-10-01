"""工位级恢复检查点与恢复判断。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

from ..config import RECOVERY_ISOLATE, RECOVERY_POLICIES, RECOVERY_RESUME
from ..store.jsonfile import JsonStore

RECOVERY_SCOPE_STATION = "station"
DOC_ID = "recovery_state"
RUNTIME_DOC_ID = "runtime_state"

OP_FILL = "fill"
OP_UNLOAD = "unload"

STATUS_INTERRUPTED = "interrupted"
STATUS_RESUMING = "resuming"
STATUS_COMPLETED = "completed"
STATUS_FINALIZED = "finalized"

# 可安全续作的阶段必须是稳定边界；阀位切换等瞬态阶段一律先隔离。
FILL_STAGE_CONFIRMED = "confirmed"
FILL_STAGE_SEATED = "seated"
FILL_STAGE_PRECOOLED = "precooled"
FILL_STAGE_FLOW_STEP = "flow_step"
FILL_STAGE_FILLED = "filled"
UNLOAD_STAGE_CONNECTED = "connected"
UNLOAD_STAGE_LATCHED = "latched"
UNLOAD_STAGE_UNLOADING = "unloading"
UNLOAD_STAGE_DRAINED = "drained"
UNLOAD_STAGE_VENTING = "venting"

FILL_RESUMABLE = {
    FILL_STAGE_CONFIRMED,
    FILL_STAGE_SEATED,
    FILL_STAGE_PRECOOLED,
    FILL_STAGE_FLOW_STEP,
}
UNLOAD_RESUMABLE = {
    UNLOAD_STAGE_CONNECTED,
    UNLOAD_STAGE_LATCHED,
    UNLOAD_STAGE_UNLOADING,
    UNLOAD_STAGE_VENTING,
}
FINAL_STAGES = {
    (OP_FILL, FILL_STAGE_FILLED),
    (OP_UNLOAD, UNLOAD_STAGE_DRAINED),
}


@dataclass
class OperationCheckpoint:
    """一个工位上一次未完全收尾的操作边界。"""

    station: str
    op_id: str
    operation: str
    stage: str
    status: str = STATUS_INTERRUPTED
    stop_requested: bool = False
    recovery_policy: str = RECOVERY_RESUME
    safe_to_resume: bool = False
    attempts: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    measured_kg: float = 0.0
    accounted_kg: float = 0.0
    remaining_kg: float = 0.0
    flow_steps_completed: int = 0
    flow_step_kg: float = 0.0
    delivered_kg: float = 0.0
    pressure_mpa: float = 0.0
    applied_actions: list[str] = field(default_factory=list)
    devices: dict[str, Any] = field(default_factory=dict)
    updated_ts: float = 0.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "OperationCheckpoint":
        allowed = cls.__dataclass_fields__.keys()
        payload = {key: value for key, value in dict(raw).items() if key in allowed}
        return cls(**payload)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def action_applied(self, action_id: str) -> bool:
        return action_id in self.applied_actions

    def mark_action(self, action_id: str) -> None:
        if action_id not in self.applied_actions:
            self.applied_actions.append(action_id)

    def terminal(self) -> bool:
        return self.status in (STATUS_COMPLETED, STATUS_FINALIZED)


@dataclass(frozen=True)
class RecoveryDecision:
    """供控制台展示和审计共用的恢复结论。"""

    scope: str
    station: str
    pending: bool
    action: str
    safe_to_resume: bool
    operation: str | None
    stage: str | None
    op_id: str | None
    stop_requested: bool
    recovery_policy: str
    measured_kg: float
    accounted_kg: float
    remaining_kg: float
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class CheckpointStore:
    """原子读写工位级恢复检查点。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def load(self, station: str) -> OperationCheckpoint | None:
        raw = self._store.read(DOC_ID)
        if not isinstance(raw, dict):
            return None
        if raw.get("scope") != RECOVERY_SCOPE_STATION:
            return None
        if raw.get("station") != station:
            return None
        checkpoint = raw.get("checkpoint")
        if not isinstance(checkpoint, dict):
            return None
        return OperationCheckpoint.from_dict(checkpoint)

    def save(self, checkpoint: OperationCheckpoint) -> None:
        self._store.write(
            DOC_ID,
            {
                "version": 1,
                "scope": RECOVERY_SCOPE_STATION,
                "station": checkpoint.station,
                "checkpoint": checkpoint.as_dict(),
            },
        )

    def clear(self, station: str) -> bool:
        if self.load(station) is None:
            return False
        self._store.remove(DOC_ID)
        return True

    def save_runtime(self, station: str, devices: dict[str, Any]) -> None:
        self._store.write(
            RUNTIME_DOC_ID,
            {"version": 1, "scope": RECOVERY_SCOPE_STATION, "station": station, "devices": devices},
        )

    def load_runtime(self, station: str) -> dict[str, Any] | None:
        raw = self._store.read(RUNTIME_DOC_ID)
        if not isinstance(raw, dict) or raw.get("scope") != RECOVERY_SCOPE_STATION:
            return None
        if raw.get("station") != station or not isinstance(raw.get("devices"), dict):
            return None
        return dict(raw["devices"])

    def clear_runtime(self, station: str) -> bool:
        if self.load_runtime(station) is None:
            return False
        self._store.remove(RUNTIME_DOC_ID)
        return True


def decide(checkpoint: OperationCheckpoint | None) -> RecoveryDecision:
    """只依据检查点给出唯一恢复结论。"""

    if checkpoint is None or checkpoint.terminal():
        return RecoveryDecision(
            scope=RECOVERY_SCOPE_STATION,
            station=checkpoint.station if checkpoint else "",
            pending=False,
            action="none",
            safe_to_resume=False,
            operation=None,
            stage=None,
            op_id=None,
            stop_requested=False,
            recovery_policy=RECOVERY_RESUME,
            measured_kg=0.0,
            accounted_kg=0.0,
            remaining_kg=0.0,
            reasons=["no_pending_operation"],
        )

    reasons: list[str] = []
    if checkpoint.operation == OP_FILL:
        stage_safe = checkpoint.stage in FILL_RESUMABLE
    elif checkpoint.operation == OP_UNLOAD:
        stage_safe = checkpoint.stage in UNLOAD_RESUMABLE
    else:
        stage_safe = False
        reasons.append("unknown_operation")

    safe = bool(checkpoint.safe_to_resume and stage_safe)
    if not checkpoint.safe_to_resume:
        reasons.append("checkpoint_marked_unsafe")
    if not stage_safe:
        reasons.append("stage_not_resumable")
    if checkpoint.stop_requested:
        safe = False
        reasons.append("stop_was_requested")
    if checkpoint.recovery_policy not in RECOVERY_POLICIES:
        safe = False
        reasons.append("invalid_recovery_policy")

    action = RECOVERY_RESUME if safe and checkpoint.recovery_policy == RECOVERY_RESUME else RECOVERY_ISOLATE
    if action == RECOVERY_ISOLATE and not reasons:
        reasons.append("station_policy_isolate")

    return RecoveryDecision(
        scope=RECOVERY_SCOPE_STATION,
        station=checkpoint.station,
        pending=True,
        action=action,
        safe_to_resume=safe,
        operation=checkpoint.operation,
        stage=checkpoint.stage,
        op_id=checkpoint.op_id,
        stop_requested=checkpoint.stop_requested,
        recovery_policy=checkpoint.recovery_policy,
        measured_kg=round(checkpoint.measured_kg, 3),
        accounted_kg=round(checkpoint.accounted_kg, 3),
        remaining_kg=round(checkpoint.remaining_kg, 3),
        reasons=reasons,
    )
