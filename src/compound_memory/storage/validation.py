"""校验与门禁谓词（机制件，#36 片 f）：ns/key/validity 校验、身份裁决。

单一定义点：门禁谓词的定义全部收在本模块；执行时序不动——参数型动词
（write/search 的 ns 校验）在方法入口、按 id 动词（feedback/revive/get）
在锁内 find 之后（ns 只有 find 后才知道），调用点留在 facade 的动词
方法内（ADR 0003 裁决 5：「门禁不散进动词模块」指谓词定义单点）。
"""

from __future__ import annotations

import datetime as dt
import re

# ns / mem_id 的路径组件白名单：两者都被直接拼进存储路径或 rglob 模式，
# 来自 LLM/宿主输出，格式不设防时 ns='agent-../../x' 可写出存储根、
# mem_id='*' 可经 rglob 命中库内任意记忆（2026-10-05 审计 P1-1/P2-3）。
# 合法 id（YYYYMMDD_hex6）与现有全部 ns 取值均落在 [A-Za-z0-9_-] 内。
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# key 是 fact/insight 同 key 更新的稳定锚点，格式约束在落库单点（_write_new，
# write/batch/distill-apply 共用）：小写字母数字段以短横线连接。原为纯文档约定、
# write 无校验，2026-10-05 单日多会话沉淀出成批日期前缀 key——日期化 key 天然
# 一次性（id 已含日期），等于放弃同 key 更新通道。日期前缀的取舍归文档，这里只守字符集与结构。
_KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def check_validity(valid_from: str | None, valid_until: str | None) -> None:
    """有效期字段校验（write 单点）：ISO date 格式 + from<=until，坏输入响亮抛 ValueError。"""
    for name, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value is None:
            continue
        try:
            dt.date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got: {value!r}")
    if valid_from and valid_until and dt.date.fromisoformat(valid_from) > dt.date.fromisoformat(valid_until):
        raise ValueError(f"valid_from {valid_from!r} is after valid_until {valid_until!r}")


def check_key(key: str | None) -> None:
    """key 格式校验（落库单点调用，write/batch/distill-apply 共用；None = 未携带，放行）。

    原 _write_new 内联校验上收至此（#36 片 f）：报错文案与时机逐位不变。"""
    if key and not _KEY_RE.match(key):
        raise ValueError(
            f"key must match {_KEY_RE.pattern} (lowercase alphanumeric segments joined by dashes), got: {key!r}"
        )


def check_project(project: str | None) -> None:
    """project slug 校验（ADR 0010）：复用 key 格式正则（小写字母数字段以短横线
    连接），写侧落库单点与检索侧入口共用本谓词；None = 未标注（全局），放行。
    空串不等价于「未携带」——落库会成为对所有读方都不可见的幽灵字段，响亮拒绝。"""
    if project is None:
        return
    if not _KEY_RE.match(project):
        raise ValueError(
            f"project must match {_KEY_RE.pattern} (lowercase alphanumeric segments joined by dashes), got: {project!r}"
        )


def check_ns(ns: str) -> None:
    """ns 格式校验（write/search 共用）：非法 ns 是调用方错误，必须抛错——
    search 侧静默返回空结果会让 agent 误判"无相关记忆"。

    字符集白名单先行于前缀检查：ns 直接拼进存储路径（active_path），
    "agent-../../x" 曾可把 .md 写出存储根（2026-10-05 审计 P1-1）——
    白名单同时封死穿越、glob 元字符与路径分隔，且必须在越权检查之前
    （恶意 ns 自证身份的 owner 校验没有资格先跑）。
    """
    if not _PATH_COMPONENT_RE.match(ns):
        raise ValueError(f"ns contains characters outside [A-Za-z0-9_-]: {ns!r}")
    if ns != "_shared" and not ns.startswith("agent-"):
        raise ValueError("ns must be '_shared' or start with 'agent-'")


def check_ns_owner(ns: str, identity: str | None, role: str = "reader") -> None:
    """读/反馈侧 owner 校验：agent-* 私有 ns 只有属主宿主可读、可反馈。

    与 write 的 `check_ns + PermissionError` 对称——写侧已保证非属主写不进
    私有 ns，读侧若不校验则任何宿主显式传 ns=agent-<别人> 即可越权读全量
    （2026-10-03 实测：search 签名原本无调用方身份参数，跨宿主零阻力）；
    feedback 侧不校验则外来 agent 可刷 uses/confidence 或复活归档。

    identity 缺省时对 _shared 放行、对 agent-* 拒绝：宁可不读，不猜身份。
    role 只是让报错指引对得上调用方的参数名（reader / agent）。
    """
    if not ns.startswith("agent-"):
        return
    owner = ns[len("agent-"):]
    if identity in (ns, owner):
        return
    raise PermissionError(
        f"namespace {ns!r} is private to {owner!r}; {role} is {identity!r}. "
        f"Pass {role}={ns!r} or {role}={owner!r} if you are that host."
    )


def resolve_identity(value: str | None, role: str, agent_id: str | None) -> str | None:
    """身份裁决：进程注入（agent_id）优先于调用方自报。

    - 未启用 attestation（agent_id 为空）⇒ 原样放行，行为同旧版（自报身份）。
    - 调用方缺省 ⇒ 自动补进程身份（诚实缺省，如 search 私有 ns 忘带 reader）。
    - 调用方与进程身份等价（agent-x / x 两种形式）⇒ 归一化为 agent_id，
      保证 validated_by 等记录字段去重一致。
    - 调用方与进程身份矛盾 ⇒ 响亮报错（伪造/配错宿主都该炸，不该静默改写）。
    """
    if agent_id is None:
        return value
    if value is None:
        return agent_id
    accepted = {agent_id, agent_id.removeprefix("agent-")}
    if value in accepted:
        return agent_id
    raise PermissionError(
        f"{role} {value!r} contradicts attested agent {agent_id!r} "
        f"(COMPOUND_MEMORY_AGENT_ID); the process identity wins"
    )
