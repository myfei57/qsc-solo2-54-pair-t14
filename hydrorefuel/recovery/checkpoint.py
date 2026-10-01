"""可恢复作业检查点。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

CHECKPOINT_VERSION = 1
RECOVERY_SCOPE = "station"

OP_UNLOAD = "unload"
OP_FILL = "fill"

POLICY_RESUME = "resume"
POLICY_ABORT = "abort"
POLICIES = (POLICY_RESUME, POLICY_ABORT)

# 各阶段必须按顺序完成；恢复时只信任最后一个已完成阶段。
UNLOAD_STAGES = (
    "init",
    "connect",
    "latch_engage",
    "open_unload",
    "transfer",
    "finish_unload",
    "vent_open",
    "vent_hold",
    "vent_close",
    "disconnect",
)
FILL_STAGES = (
    "init",
    "confirm",
    "seat_nozzle",
    "precool",
    "open_fill_valve",
    "transfer",
    "fill_complete",
    "isolated",
    "stopped",
    "unseat_nozzle",
)

# 可以安全续作的最后已完成阶段。其他阶段一律先隔离收尾。
SAFE_UNLOAD_STAGES = {
    "init",
    "connect", "latch_engage", "open_unload", "transfer", "finish_unload", "vent_open", "vent_hold"}
SAFE_FILL_STAGES = {"init", "confirm", "seat_nozzle", "precool", "open_fill_valve", "transfer"}


@dataclass(frozen=True)
class Checkpoint:
    """一个工位上未完成作业的持久化状态。"""

    version: int
    station: str
    op_id: str
    operation: str
    stage: str
    policy: str
    interrupted: bool
    created_ts: float
    updated_ts: float
    vehicle: str = ""
    target_kg: float = 0.0
    completed_kg: float = 0.0
    accounted_kg: float = 0.0
    meter_total_kg: float = 0.0
    steps_total: int = 0
    steps_requested: int = 0
    step_completed: int = 0
    step_current_kg: float = 0.0
    rate_kg_per_min: float = 0.0
    dt: float = 1.0
    hose_pressure_mpa: float = 0.0
    truck_pressure_mpa: float = 0.0
    vent_open: bool = False
    vented_mpa: float = 0.0
    params: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        station: str,
        op_id: str,
        operation: str,
        stage: str,
        policy: str,
        now: float,
        **params: Any,
    ) -> "Checkpoint":
        if operation not in (OP_UNLOAD, OP_FILL):
            raise ValueError("未知的可恢复作业类型")
        if policy not in POLICIES:
            raise ValueError("未知的工位恢复策略")
        stages = UNLOAD_STAGES if operation == OP_UNLOAD else FILL_STAGES
        if stage not in stages:
            raise ValueError("未知的作业阶段")
        payload = dict(params)
        nested_params = payload.pop("params", None)
        if isinstance(nested_params, dict):
            payload.update(nested_params)
        known = {name: payload.pop(name) for name in (
            "vehicle",
            "target_kg",
            "completed_kg",
            "accounted_kg",
            "meter_total_kg",
            "steps_total",
            "steps_requested",
            "step_completed",
            "step_current_kg",
            "rate_kg_per_min",
            "dt",
            "hose_pressure_mpa",
            "truck_pressure_mpa",
            "vent_open",
            "vented_mpa",
        ) if name in payload and name != "params"}
        return cls(
            version=CHECKPOINT_VERSION,
            station=station,
            op_id=op_id,
            operation=operation,
            stage=stage,
            policy=policy,
            interrupted=False,
            created_ts=float(now),
            updated_ts=float(now),
            params=payload,
            **known,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Checkpoint":
        data = dict(raw)
        params = dict(data.get("params", {}))
        allowed = {field_.name for field_ in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        values: dict[str, Any] = {"params": params}
        for key in allowed - {"params"}:
            values[key] = data.get(key)
        checkpoint = cls(**values)
        checkpoint._validate()
        return checkpoint

    def _validate(self) -> None:
        if self.version != CHECKPOINT_VERSION:
            raise ValueError("不兼容的检查点版本")
        if not self.station:
            raise ValueError("检查点必须按工位标识")
        if self.operation not in (OP_UNLOAD, OP_FILL):
            raise ValueError("未知的可恢复作业类型")
        if self.policy not in POLICIES:
            raise ValueError("未知的工位恢复策略")
        stages = UNLOAD_STAGES if self.operation == OP_UNLOAD else FILL_STAGES
        if self.stage not in stages:
            raise ValueError("未知的作业阶段")

    @property
    def stages(self) -> tuple[str, ...]:
        return UNLOAD_STAGES if self.operation == OP_UNLOAD else FILL_STAGES

    @property
    def stage_index(self) -> int:
        return self.stages.index(self.stage)

    @property
    def terminal(self) -> bool:
        return self.stage == self.stages[-1]

    @property
    def can_resume(self) -> bool:
        """当前最后已完成阶段是否允许安全续作。"""

        safe = SAFE_UNLOAD_STAGES if self.operation == OP_UNLOAD else SAFE_FILL_STAGES
        return not self.terminal and self.stage in safe

    @property
    def decision(self) -> str:
        """工位策略和现场安全条件合并后的恢复动作。"""

        if self.terminal:
            return POLICY_ABORT
        return POLICY_RESUME if self.policy == POLICY_RESUME and self.can_resume else POLICY_ABORT

    def advanced(self, stage: str, now: float, **changes: Any) -> "Checkpoint":
        """返回推进到某阶段后的新不可变检查点。"""

        if stage not in self.stages:
            raise ValueError("未知的作业阶段")
        if self.stages.index(stage) < self.stage_index:
            raise ValueError("不能回退检查点")
        data = asdict(self)
        updated_params = changes.pop("params", None)
        data["params"] = dict(self.params)
        if updated_params is not None:
            data["params"].update(updated_params)
        data.update(changes)
        data["stage"] = stage
        data["updated_ts"] = float(now)
        return Checkpoint.from_dict(data)

    def with_interrupt(self, now: float) -> "Checkpoint":
        return self.advanced(self.stage, now, interrupted=True)

    def as_dict(self) -> dict:
        return asdict(self)

    def decision_record(self) -> dict:
        """恢复判断的统一口径：控制台展示与审计共用。"""

        return {
            "scope": RECOVERY_SCOPE,
            "station": self.station,
            "op_id": self.op_id,
            "operation": self.operation,
            "stage": self.stage,
            "stage_index": self.stage_index,
            "stage_total": len(self.stages),
            "policy": self.policy,
            "safe_to_resume": self.can_resume,
            "decision": self.decision,
            "interrupted": self.interrupted,
            "completed_kg": round(self.completed_kg, 6),
            "remaining_kg": round(max(0.0, self.target_kg - self.completed_kg), 6) if self.target_kg else 0.0,
            "updated_ts": round(self.updated_ts, 3),
        }
