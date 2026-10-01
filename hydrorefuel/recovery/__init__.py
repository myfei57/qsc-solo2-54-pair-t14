"""断电与手动停止后的断点恢复。

恢复范围固定按工位（``station``）判定，而不是按单条管路：一个工位同一时刻
只允许有一个未完成作业，恢复判断、计量口径和审计记录都使用同一个持久化
检查点。检查点在每个已完成阶段后原子落盘，因此续作只能从最后一个完整阶段
之后开始，不会把已经计量的质量再提交一次。
"""

from __future__ import annotations

from .checkpoint import (
    CHECKPOINT_VERSION,
    OP_FILL,
    OP_UNLOAD,
    POLICY_ABORT,
    POLICY_RESUME,
    RECOVERY_SCOPE,
    Checkpoint,
)
from .manager import RecoveryManager
from .resume import resume_fill, resume_unload

__all__ = [
    "CHECKPOINT_VERSION",
    "OP_FILL",
    "OP_UNLOAD",
    "POLICY_ABORT",
    "POLICY_RESUME",
    "RECOVERY_SCOPE",
    "Checkpoint",
    "RecoveryManager",
    "resume_fill",
    "resume_unload",
]
