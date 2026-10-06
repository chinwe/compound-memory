"""Mock 记忆生成器：为 perf-bench 造规模可控、内容多样的记忆库。

- 主题簇结构：记忆分摊到 TOPICS 各簇，bench 查询用簇词构造窄/宽候选集，
  延迟数字因此可解释（词面候选集 ≈ 命中簇的文档数）；
- 字段分布贴近真实使用：type 权重（fact/insight/episode/skill）、
  confidence 0.3-1.0、uses 偏小值、created 跨过去 180 天、fact/insight 带 key；
- 确定性：固定 random seed，同参数生成序列一致（bench 可重复）；
- 防呆：CLI 拒绝写真实记忆库根目录（mock 数据污染真实库是真实事故风险）。

独立用法：
    uv run python experiments/perf-bench/gen_mock.py --root /tmp/mock-mem --count 500
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path
from typing import Any

# 中英混合主题簇：簇名 + 簇内子词（内容与查询共用同一词表，保证词面命中可控）
TOPICS: list[tuple[str, list[str]]] = [
    ("redis", ["redis", "缓存", "persistence", "淘汰策略", "aof", "rdb", "过期"]),
    ("nginx", ["nginx", "反向代理", "buffer", "upstream", "限流", "rewrite"]),
    ("docker", ["docker", "镜像", "prune", "网络", "bridge", "compose"]),
    ("k8s", ["kubernetes", "pod", "调度", "探针", "deployment", "滚动更新"]),
    ("python", ["python", "uv", "虚拟环境", "pytest", "类型标注", "打包"]),
    ("typescript", ["typescript", "tsconfig", "严格模式", "类型收窄", "枚举"]),
    ("vercel", ["vercel", "serverless", "超时", "edge", "部署", "hobby"]),
    ("nextjs", ["next.js", "ssr", "路由", "增量渲染", "app router"]),
    ("spring", ["spring", "security", "webauthn", "过滤器", "会话"]),
    ("git", ["git", "rebase", "cherry-pick", "子模块", "hook", "冲突"]),
    ("macos", ["macos", "launchd", "plist", "homebrew", "沙箱", "权限"]),
    ("sqlite", ["sqlite", "事务", "wal", "索引", "pragma", "迁移"]),
    ("邮件", ["邮件", "smtp", "收件", "过滤器", "归档", "模板"]),
    ("文档", ["文档", "腾讯文档", "飞书", "目录结构", "命名规范"]),
    ("学校", ["学校", "小学", "幼儿园", "校历", "通知", "作业"]),
    ("日程", ["日程", "提醒", "日历", "排期", "冲突", "时间块"]),
    ("网络", ["dns", "代理", "vpn", "端口", "防火墙", "转发"]),
    ("监控", ["监控", "告警", "zabbix", "面板", "阈值", "巡检"]),
    ("安全", ["密钥", "轮换", "token", "凭证", "加密", "备份"]),
    ("ai", ["embedding", "向量", "模型", "推理", "量化", "上下文"]),
]

# 跨簇高频词：混进每条内容，供 bench 的宽查询构造大候选集
CROSS_WORDS = ["配置", "部署", "问题", "方案"]

# 每簇内可能出现在宽查询里的占比控制不了，直接把跨簇词随机拼进内容
TEMPLATES = [
    "{head}相关配置要点：{body}。",
    "关于{head}的部署经验，{body}。",
    "{head}排查记录：{body}。",
    "备忘：{head}场景下，{body}。",
]

TYPE_WEIGHTS = [("fact", 50), ("insight", 20), ("episode", 20), ("skill", 10)]

# bench 查询词表（与 TOPICS 对应：narrow 用簇主词，broad 用跨簇词）
NARROW_QUERIES = ["redis 持久化策略", "nginx 反向代理配置", "kubernetes pod 调度", "邮件归档规则"]
BROAD_QUERIES = ["配置 问题", "部署 方案", "配置 部署"]
# 语义改写查询：与簇词近零词面重叠，供向量路（延迟主要看编码 + KNN）
SEMANTIC_QUERIES = ["内存数据库怎么保证不丢数据", "网关转发参数怎么调", "容器编排怎么分配合适节点"]


def make_memories(count: int, seed: int = 42, today: dt.date | None = None) -> list[dict[str, Any]]:
    """生成 count 条 write kwargs；同 (count, seed) 序列一致。"""
    rng = random.Random(seed)
    today = today or dt.date.today()
    type_pool: list[str] = []
    for mtype, weight in TYPE_WEIGHTS:
        type_pool.extend([mtype] * weight)
    out: list[dict[str, Any]] = []
    for i in range(count):
        topic_name, subwords = TOPICS[i % len(TOPICS)]
        head = rng.choice(subwords[:2])
        # 子短语数量决定内容长度（约 50-300 字符，贴近真实记忆分布）
        body_items = rng.sample(subwords, k=rng.randint(2, min(6, len(subwords))))
        body_items += rng.choices(CROSS_WORDS, k=rng.randint(1, 4))
        filler = "，涉及" + "、".join(body_items) + "的取舍"
        content = rng.choice(TEMPLATES).format(head=head, body=filler) + rng.choice(CROSS_WORDS) + "处理优先级见记录。"
        mtype = rng.choice(type_pool)
        created = today - dt.timedelta(days=rng.randint(0, 180))
        ns = "_shared" if rng.random() < 0.9 else "agent-bench"
        mem: dict[str, Any] = {
            "content": content,
            "type": mtype,
            # 私有 ns 的写入者必须是属主（agent-bench ⇒ bench），否则 store 权限校验拒绝
            "source": "bench-seeder" if ns == "_shared" else "bench",
            "ns": ns,
            "created": created.isoformat(),
            "confidence": round(rng.uniform(0.3, 1.0), 2),
        }
        if mtype in ("fact", "insight"):
            # key 白名单：小写字母数字 + 连字符（斜杠分段与中文主题名会被 check_key 拒绝）
            mem["key"] = f"bench-t{i % len(TOPICS)}-{rng.randint(0, 4)}"
        out.append(mem)
    return out


def seed_store(root: Path, count: int, seed: int = 42) -> list[str]:
    """写入 count 条 mock 记忆（git 关闭加速 seed；bench 的 write 场景单独开 git）。"""
    from compound_memory.storage import MemoryStore

    store = MemoryStore(root, git=False)
    ids = []
    for mem in make_memories(count, seed):
        ids.append(store.write(**mem)["id"])
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a mock memory store for perf-bench")
    parser.add_argument("--root", type=Path, required=True, help="target store root (must not be the real memory root)")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    real_root = Path.home() / ".agents" / "memory"
    if args.root.resolve() == real_root.resolve():
        print(f"refusing to seed the real memory root: {real_root}", file=sys.stderr)
        return 1
    ids = seed_store(args.root, args.count, args.seed)
    print(f"seeded {len(ids)} memories -> {args.root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
