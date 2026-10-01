"""断点续作与安全隔离。

恢复口径固定按工位（``station``）判断和持久化，不按单条管路拆分。每个工位
最多保留一个可恢复操作检查点，检查点与设备运行态、计量台账在同一份原子文档
中落盘，保证控制台展示、恢复判断和审计使用同一事实来源。
"""

from .checkpoint import (
    RECOVERY_SCOPE_STATION,
    CheckpointStore,
    OperationCheckpoint,
    RecoveryDecision,
)
from .runtime import RecoveryRuntime

__all__ = [
    "RECOVERY_SCOPE_STATION",
    "CheckpointStore",
    "OperationCheckpoint",
    "RecoveryDecision",
    "RecoveryRuntime",
]
