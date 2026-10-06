"""存储层包：Markdown + YAML frontmatter 存储、命名空间、git、复利引擎。

根目录布局：
    namespaces/<ns>/<type>/<id>.md   活动记忆
    archive/<ns>/<type>/<id>.md      衰减归档（可恢复）
    index/tokens.json                可重建的词法检索缓存
    index/vectors.db                 可重建的向量检索缓存（vec extra，缺失时自动降级）
    review-queue.md                  fact/insight 冲突队列

包结构（ADR 0003）：facade.py 承载 MemoryStore；机制层五件（paths/files/
gitlayer/locking/validation）随 #36 逐件外移；动词层七件归动词票。本
__init__ 全量 re-export 旧 storage.py 的模块级导入面——包外（tests/cli/
server/extraction）零改动；新代码用规范路径（如 compound_memory.storage.facade）。
"""

from __future__ import annotations

# 旧 storage.py 顶层 import 的 stdlib 模块引用（tests 经 storage_mod.os /
# storage_mod.subprocess 打补丁——os/subprocess 是进程级单例，补丁全局生效，
# 这里只为包模块保留旧命名空间属性，兼容既有测试的触达路径）
import os
import subprocess

from .facade import (
    ARCHIVE_USES_THRESHOLD,
    CONF_CROSS_AGENT_BUMP,
    CONF_USE_BUMP,
    CONFIDENCE_HISTOGRAM_BUCKETS,
    DISTILL_DUP_SIM_THRESHOLD,
    GIT_IDENTITY,
    GIT_LOCK_RETRY_DELAYS,
    MEMORY_TYPES,
    PROMOTION_USES_THRESHOLD,
    RECENT_WINDOW_DAYS,
    USES_HISTOGRAM_BUCKETS,
    VEC_POOL,
    _Batch,
    _PATH_COMPONENT_RE,
    _KEY_RE,
    MemoryStore,
    _check_validity,
    _conf_bucket,
    _git_available,
    _unlink_file,
    _uses_bucket,
    _within_days,
)
from .paths import default_root
