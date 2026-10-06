"""存储层包：Markdown + YAML frontmatter 存储、命名空间、git、复利引擎。

根目录布局：
    namespaces/<ns>/<type>/<id>.md   活动记忆
    archive/<ns>/<type>/<id>.md      衰减归档（可恢复）
    index/tokens.json                可重建的词法检索缓存
    index/vectors.db                 可重建的向量检索缓存（vec extra，缺失时自动降级）
    review-queue.md                  fact/insight 冲突队列

包结构（ADR 0003 终态，#36/#37/#38/#39）：facade.py 承载 MemoryStore
（构造装配 + get/link 方法体 + 机制薄委托 + 动词一行转发）；机制层五件
（paths/files/gitlayer/locking/validation）与动词层七件（stats/review/
distill/search/indexing/writing/lifecycle）各为单一定义点。

包级公开导入面（server/cli 消费面）收窄为五个名字：MemoryStore /
default_root / MEMORY_TYPES / DISTILL_DUP_SIM_THRESHOLD /
PROMOTION_USES_THRESHOLD。其余旧 re-export 名已移除，模块规范路径是
唯一入口：桶函数→storage.stats，_unlink_file→storage.files，
GIT_IDENTITY/GIT_LOCK_RETRY_DELAYS/_git_available→storage.gitlayer，
_Batch→storage.locking，_PATH_COMPONENT_RE/_KEY_RE/check_validity→
storage.validation，VEC_POOL→storage.search，生命周期阈值→storage.lifecycle。
"""

from __future__ import annotations

from ..model import MEMORY_TYPES
from .distill import DISTILL_DUP_SIM_THRESHOLD, PROMOTION_USES_THRESHOLD
from .facade import MemoryStore
from .paths import default_root

__all__ = [
    "DISTILL_DUP_SIM_THRESHOLD",
    "MEMORY_TYPES",
    "MemoryStore",
    "PROMOTION_USES_THRESHOLD",
    "default_root",
]
