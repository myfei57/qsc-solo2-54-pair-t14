"""按工位保存与判定恢复检查点。"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..store.jsonfile import JsonStore
from .checkpoint import RECOVERY_SCOPE, Checkpoint, POLICY_RESUME

DOC_ID = "recovery_checkpoint"


class RecoveryManager:
    """每个工位一个原子检查点文档；本平台当前实例代表一个工位。"""

    def __init__(self, store: JsonStore, clock: Clock, station: str, policy: str) -> None:
        self._store = store
        self._clock = clock
        self._station = station
        self._policy = policy if policy in ("resume", "abort") else POLICY_RESUME
        self._lock = threading.RLock()
        self._checkpoint: Checkpoint | None = None
        self._load()

    @property
    def scope(self) -> str:
        return RECOVERY_SCOPE

    @property
    def policy(self) -> str:
        return self._policy

    def get(self) -> Checkpoint | None:
        with self._lock:
            return self._checkpoint

    def save(self, checkpoint: Checkpoint) -> Checkpoint:
        """保存续作函数返回的新阶段检查点。"""

        with self._lock:
            if self._checkpoint is None or checkpoint.op_id != self._checkpoint.op_id:
                raise ValueError("不能保存其他工位作业的检查点")
            self._save(checkpoint)
            return checkpoint

    def exists(self) -> bool:
        return self.get() is not None

    def start(self, *, op_id: str, operation: str, stage: str, **fields: object) -> Checkpoint:
        with self._lock:
            if self._checkpoint is not None and not self._checkpoint.terminal:
                raise ValueError("该工位已有未完成作业，不能开启新作业")
            checkpoint = Checkpoint.create(
                station=self._station,
                op_id=op_id,
                operation=operation,
                stage=stage,
                policy=self._policy,
                now=self._clock.now(),
                **fields,
            )
            self._save(checkpoint)
            return checkpoint

    def advance(self, stage: str, persist: bool = True, **changes: object) -> Checkpoint:
        with self._lock:
            checkpoint = self._require()
            checkpoint = checkpoint.advanced(stage, self._clock.now(), **changes)
            if persist:
                self._save(checkpoint)
            return checkpoint

    def update(self, persist: bool = True, **changes: object) -> Checkpoint:
        """更新当前阶段内的计量字段，不改变已完成阶段。"""

        with self._lock:
            checkpoint = self._require().advanced(self._require().stage, self._clock.now(), **changes)
            if persist:
                self._save(checkpoint)
            return checkpoint

    def mark_interrupted(self) -> Checkpoint | None:
        with self._lock:
            if self._checkpoint is None or self._checkpoint.terminal:
                return self._checkpoint
            checkpoint = self._checkpoint.with_interrupt(self._clock.now())
            self._save(checkpoint)
            return checkpoint

    def clear(self) -> None:
        with self._lock:
            self._checkpoint = None
            self._store.remove(DOC_ID)

    def decision(self) -> dict | None:
        checkpoint = self.get()
        return None if checkpoint is None else checkpoint.decision_record()

    def mutate(self, callback: Callable[[Checkpoint], Checkpoint]) -> Checkpoint:
        """在同一把锁内完成读-改-存，保证重复触发不会重复计量。"""

        with self._lock:
            checkpoint = callback(self._require())
            self._save(checkpoint)
            self._checkpoint = checkpoint
            return checkpoint

    def _require(self) -> Checkpoint:
        if self._checkpoint is None:
            raise ValueError("没有可恢复的工位作业")
        if self._checkpoint.station != self._station:
            raise ValueError("检查点不属于当前工位")
        return self._checkpoint

    def _save(self, checkpoint: Checkpoint) -> None:
        if checkpoint.station != self._station:
            raise ValueError("恢复检查点必须按工位保存")
        self._store.write(DOC_ID, checkpoint.as_dict())
        self._checkpoint = checkpoint

    def _load(self) -> None:
        raw = self._store.read(DOC_ID)
        if raw is None:
            return
        checkpoint = Checkpoint.from_dict(raw)
        if checkpoint.station != self._station:
            # 存储目录被拿到别的工位使用时，宁可不自动恢复，也不能串工位续作。
            self._checkpoint = None
            return
        self._checkpoint = checkpoint
