"""抽取清单扫描器：会话 transcript → 记忆候选（抽取管线的确定性段，零 LLM）。

蒸馏三段式的第二应用（spec：确定性准备自动跑、判断由 Agent 完成）：
扫描只「发现候选」，写库仍由 Agent 逐条确认走 memory_write——清单是建议、
写入是动作，同 key 冲突照常进 review 队列（不静默原则在管线里不变）。

transcript 解析支持 ZCode model-io jsonl：每行是一次 API 调用快照（完整
messages 历史），user content 为块列表且首个 text 块多为 system-reminder/
hook 注入——注入块按标记过滤，跨快照重复的用户话保序去重。

模式匹配面向中文宿主场景：statement 模式 → fact 候选、pitfall 模式 →
insight 候选；提供 store 时对 _shared 做词面去重标注（likely_dup_of 指向
既有条目，Agent 复用同 key 而非新开条目），命中相似仍进清单——丢弃与否
是判断段的事，扫描器不静默吞。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .model import Memory
from .scoring import doc_text, tokenize
from .storage import MemoryStore

# 声明类模式（用户陈述事实/偏好/环境）→ fact 候选；踩坑类 → insight 候选。
# 面向中文宿主场景；英文会话 P0 不覆盖（模式表后续按需扩充）。
STATEMENT_PATTERNS = (
    "我用", "我用的是", "默认用", "以后都", "记住", "偏好",
    "换成", "部署在", "装了", "升级了", "安装了", "部署了", "迁移到",
    "地址是", "密码是", "账号是", "版本是", "端口是",
)
PITFALL_PATTERNS = (
    # 否定指令优先于泛坑描述：「不要用 X」比「有坑」信号更明确
    "不要用", "别用", "报错", "踩坑", "坑是", "有坑", "失败", "不行",
    "问题出在", "注意", "超时", "限制是",
)

# 注入块标记：命中即整块跳过（hook 注入、系统提醒、通知、命令展开都不是用户话）
INJECTION_MARKERS = (
    "<system-reminder>",
    "[SYSTEM NOTIFICATION",
    "<task-notification",
    "<command-name>",
    "<local-command",
    "Caveat:",
)

MAX_CANDIDATES = 20  # 每次扫描的清单上限：防喋喋不休的会话产出垃圾清单
QUOTE_CHARS = 200  # 候选摘录截断
# 去重标注阈值：查询 token 被库内条目覆盖率（containment）。不用 normalized BM25——
# 长句查询的分母惩罚使复述句也只有 ~0.12，结构性偏低；覆盖率对「复述检测」语义正确
EXTRACT_DUP_COVERAGE = 0.5
SENTENCE_SPLIT = "。！？!?；;\n"


def user_texts_from_model_io(path: Path) -> list[str]:
    """ZCode model-io jsonl → 真实用户话（保序去重）。

    每行一个快照、每快照带全量历史，因此同一句话会出现多次——dict.fromkeys
    保序去重；注入块（system-reminder/hook/通知）整块跳过。
    """
    texts: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过：宁少一条输入，不让整场扫描崩掉
            messages = ((event.get("request") or {}).get("body") or {}).get("messages") or []
            for message in messages:
                if message.get("role") != "user":
                    continue
                content = message.get("content")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = (block.get("text") or "").strip()
                    if not text or text.startswith(INJECTION_MARKERS):
                        continue
                    if text not in seen:
                        seen.add(text)
                        texts.append(text)
    return texts


def _sentences(text: str) -> list[str]:
    buf: list[str] = []
    for piece in text.split(SENTENCE_SPLIT):
        piece = piece.strip()
        if piece:
            buf.append(piece)
    return buf


def _match_pattern(sentence: str) -> tuple[str, str] | None:
    """返回 (suggested_type, signal) 或 None；statement 优先于 pitfall。"""
    for pattern in STATEMENT_PATTERNS:
        if pattern in sentence:
            return "fact", pattern
    for pattern in PITFALL_PATTERNS:
        if pattern in sentence:
            return "insight", pattern
    return None


def _dup_of(store: MemoryStore, quote: str) -> str | None:
    """库内（仅 _shared）复述标注：查询 token 被单条条目覆盖率最高者达阈值即标注。

    私有 ns 无身份不读（访问控制不变量）；覆盖率低于阈值返回 None——漏标由
    Agent 自行 search 兜底，扫描器不静默吞候选。
    """
    q_tokens = set(tokenize(quote))
    if not q_tokens:
        return None
    candidates = store._candidates(sorted(q_tokens), {"_shared"})
    best_id, best_cov = None, 0.0
    for mem in candidates:
        coverage = len(q_tokens & set(tokenize(doc_text(mem)))) / len(q_tokens)
        if coverage > best_cov:
            best_id, best_cov = mem.id, coverage
    return best_id if best_cov >= EXTRACT_DUP_COVERAGE else None


def scan_texts(
    texts: list[str],
    store: MemoryStore | None = None,
    max_items: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    """用户话 → 候选清单：分句、模式匹配、去重标注、上限截断。

    候选字段：quote（原句摘录）、suggested_type、signal（命中模式）、
    likely_dup_of（既有条目 id 或 None）。key 由 Agent 判断时定——扫描器
    不猜 key（spec 写入约定：复用既有 key 依赖对库内现状的判断）。
    """
    candidates: list[dict[str, Any]] = []
    for text in texts:
        for sentence in _sentences(text):
            matched = _match_pattern(sentence)
            if matched is None:
                continue
            suggested_type, signal = matched
            quote = sentence[:QUOTE_CHARS] + ("…" if len(sentence) > QUOTE_CHARS else "")
            candidates.append(
                {
                    "quote": quote,
                    "suggested_type": suggested_type,
                    "signal": signal,
                    "likely_dup_of": _dup_of(store, quote) if store is not None else None,
                }
            )
            if len(candidates) >= max_items:
                return candidates
    return candidates


def extract(transcript: Path, store: MemoryStore) -> dict[str, Any]:
    """入口：解析 transcript、扫描、清单落 <root>/extract/last-candidates.json。

    extract/ 是运行时工件目录（_ensure_layout 统一 gitignore）——清单含会话
    摘录，不进记忆库的审计史；返回摘要供 CLI 打印。
    """
    texts = user_texts_from_model_io(transcript)
    candidates = scan_texts(texts, store=store)
    out_dir = store.root / "extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "last-candidates.json"
    manifest = {
        "generated": dt.date.today().isoformat(),
        "source": str(transcript),
        "candidates": candidates,
    }
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"candidates": len(candidates), "out": str(out_path), "source": str(transcript)}
