"""抽取清单扫描器：会话 transcript → 记忆候选（抽取管线的确定性段，零 LLM）。

蒸馏三段式的第二应用（spec：确定性准备自动跑、判断由 Agent 完成）：
扫描只「发现候选」，写库仍由 Agent 逐条确认走 memory_write——清单是建议、
写入是动作，同 key 冲突照常进 review 队列（不静默原则在管线里不变）。

transcript 解析支持两种宿主格式，按内容形状分发（不靠文件名约定）：

1. **session log**（WorkBuddy 主源）：`<project>/<sessionId>.jsonl`，逐轮完整
   消息（type=message / role / content[].text）。真实用户话被 `<user_query>` 或
   `<session>` 包裹，同块内混着注入块（user-context / team-context / 队友消息 /
   上下文压缩摘要）——壳剥掉、注入块整块跳过。
2. **model-io jsonl**（ZCode）：每行是一次 API 调用快照（完整 messages 历史），
   user content 为块列表且首个 text 块多为 system-reminder/hook 注入；跨快照
   重复的用户话保序去重。

**刻意不支持 trace**（`~/.workbuddy/traces/<pid>/trace_*.json`）：generation
span 的 toolInput 是请求快照，但被**头部**硬截到 100000 字符，整段解析必抛；
即便逐条 raw_decode 抢救（实测 845/845 span 成功），单快照也只剩**首轮** user
消息，多轮会话损失严重。接它会产出"看起来扫描过、实际漏掉大部分会话"的假阴性，
比明确不支持更有害。traces/ 只作排障线索，不是抽取源。

模式匹配面向中文宿主场景：statement 模式 → fact 候选、pitfall 模式 →
insight 候选；提供 store 时对 _shared 做词面去重标注（likely_dup_of 指向
既有条目，Agent 复用同 key 而非新开条目），命中相似仍进清单——丢弃与否
是判断段的事，扫描器不静默吞。
"""

from __future__ import annotations

import datetime as dt
import json
import re
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
    "<system-reminder",
    "[SYSTEM NOTIFICATION",
    "<task-notification",
    "<command-name>",
    "<local-command",
    "Caveat:",
    # WorkBuddy 形态：Agent Team 注入、队友派活、上下文压缩摘要、续写指令
    "<teammate-message",
    "<user-prompt-submit-hook",
    "<conversation_history_summary>",
    "Please continue with the conversation based on the summarized context",
    "You are a prompt enhancement assistant",
)

# 真实用户话外壳：WorkBuddy 把用户输入包在 <user_query>（主路）或 <session>
# （远程/小程序回传路径）里；先剥壳再判注入，否则整块会被壳掩盖成"非注入"。
USER_WRAPPERS = (
    re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S),
    re.compile(r"<session>\s*(.*?)\s*</session>", re.S),
)

MAX_CANDIDATES = 20  # 每次扫描的清单上限：防喋喋不休的会话产出垃圾清单
EXTRACT_MAX_SESSIONS = 500  # 批量模式单次最多吃多少个会话文件（防目录爆量）
QUOTE_CHARS = 200  # 候选摘录截断
# 去重标注阈值：查询 token 被库内条目覆盖率（containment）。不用 normalized BM25——
# 长句查询的分母惩罚使复述句也只有 ~0.12，结构性偏低；覆盖率对「复述检测」语义正确
EXTRACT_DUP_COVERAGE = 0.5
SENTENCE_SPLIT = "。！？!?；;\n"


def _texts_from_messages(messages: Any) -> list[str]:
    """messages 数组 → 剥壳去注入后的真实用户话块。"""
    out: list[str] = []
    if not isinstance(messages, list):
        return out
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in ("text", "input_text"):
                continue
            cleaned = _unwrap_user_text((block.get("text") or "").strip())
            if cleaned:
                out.append(cleaned)
    return out


def _unwrap_user_text(text: str) -> str | None:
    """剥 <user_query>/<session> 壳并滤注入块；不是用户话返回 None。

    壳优先于注入判定：用户话整体被壳包裹，若先按整块判注入会漏掉真实输入。
    壳内再判注入（壳里塞 system-reminder 的形态确实存在）。
    """
    if not text:
        return None
    for wrapper in USER_WRAPPERS:
        matched = wrapper.search(text)
        if matched:
            inner = matched.group(1).strip()
            if not inner or inner.startswith(INJECTION_MARKERS):
                return None
            return inner
    if text.startswith(INJECTION_MARKERS):
        return None
    return text


def _dedupe(texts: list[str]) -> list[str]:
    return list(dict.fromkeys(texts))


def user_texts_from_model_io(path: Path) -> list[str]:
    """ZCode model-io jsonl → 真实用户话（保序去重）。

    每行一个快照、每快照带全量历史，因此同一句话会出现多次——dict.fromkeys
    保序去重；注入块（system-reminder/hook/通知）整块跳过。
    """
    texts: list[str] = []
    for event in _iter_json_lines(path):
        body = ((event.get("request") or {}).get("body") or {}) if isinstance(event, dict) else {}
        texts.extend(_texts_from_messages(body.get("messages") or []))
    return _dedupe(texts)


def user_texts_from_session_log(path: Path) -> list[str]:
    """WorkBuddy session log jsonl → 真实用户话（保序去重）。

    每行一个事件，只取 type=="message" && role=="user" 的 input_text 块——
    function_call / reasoning / file-history-snapshot 等事件不是用户话。
    """
    texts: list[str] = []
    for event in _iter_json_lines(path):
        if not isinstance(event, dict) or event.get("type") != "message" or event.get("role") != "user":
            continue
        texts.extend(_texts_from_messages([event]))
    return _dedupe(texts)


def _iter_json_lines(path: Path) -> list[Any]:
    """逐行读 jsonl，坏行跳过（宁少一条输入，不让整场扫描崩掉）。"""
    events: list[Any] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


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


DETECT_HEAD_CHARS = 65536  # 形态探测只读文件头：足够看清结构，避开大文件全读
DETECT_MAX_LINES = 20  # 最多探这么行（较新会话以多条 session-meta 开头）


def detect_transcript_kind(path: Path) -> str:
    """按内容形状判定 transcript 形态：session-log / model-io / unsupported。

    不靠文件名约定——两种宿主都把日志叫 .jsonl。也不只看第一行：较新会话以
    session-meta 事件开头（实测 92 个真实会话里 16 个如此，且恰好是最近的），
    只看首行会把它们全判成 unsupported 静默跳过。

    trace 显式判为 unsupported（而非 unknown）：它的 toolInput 头部截断到
    100000 字符，单快照只剩首轮 user 消息，接进来是"看似扫过、实则大面积漏"。
    给出明确不支持的理由，报错直接指向唯一受支持的主源。
    """
    head = path.read_text(encoding="utf-8", errors="replace")[:DETECT_HEAD_CHARS]
    if '"spans"' in head and '"trace"' in head:
        return "unsupported"
    for line in head.splitlines()[:DETECT_MAX_LINES]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message":
            return "session-log"
        if "request" in event:
            return "model-io"
    return "unsupported"


PARSERS = {
    "session-log": user_texts_from_session_log,
    "model-io": user_texts_from_model_io,
}

UNSUPPORTED_HINT = (
    "不支持的 transcript 形态：{path}。受支持的源有——"
    "WorkBuddy session log（~/.workbuddy/projects/<项目>/<sessionId>.jsonl）"
    "或 ZCode model-io jsonl；传目录则批量扫全部 session log。"
    "WorkBuddy traces/ 不接入（toolInput 头部截断，只剩首轮用户话）。"
)


def extract(transcript: Path, store: MemoryStore) -> dict[str, Any]:
    """入口：解析 transcript、扫描、清单落 <root>/extract/last-candidates.json。

    extract/ 是运行时工件目录（_ensure_layout 统一 gitignore）——清单含会话
    摘录，不进记忆库的审计史；返回摘要供 CLI 打印。
    """
    kind = detect_transcript_kind(transcript)
    parser = PARSERS.get(kind)
    if parser is None:
        raise ValueError(UNSUPPORTED_HINT.format(path=transcript))
    texts = parser(transcript)
    candidates = scan_texts(texts, store=store)
    return _write_manifest(store, transcript, kind, texts, candidates)


def _write_manifest(
    store: MemoryStore,
    source: str | Path,
    kind: str,
    texts: list[str],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    out_dir = store.root / "extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "last-candidates.json"
    manifest = {
        "generated": dt.date.today().isoformat(),
        "source": str(source),
        "parser": kind,
        "user_turns": len(texts),
        "candidates": candidates,
    }
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "candidates": len(candidates),
        "user_turns": len(texts),
        "parser": kind,
        "out": str(out_path),
        "source": str(source),
    }


def extract_dir(
    root: Path,
    store: MemoryStore,
    max_sessions: int = EXTRACT_MAX_SESSIONS,
) -> dict[str, Any]:
    """批量扫一个宿主日志目录（WorkBuddy: ~/.workbuddy/projects）。

    只吃一级会话文件，**跳过 subagents/**：那里的 role=="user" 其实是
    team-lead agent 的派活文本（"用户俊伟想要…" 是第三人称转述，不是本人
    陈述），实测 3/3 候选全是噪声——混进来只会污染清单。

    跨会话合并去重后再扫（同一句话在多会话复述），单会话上限由
    scan_texts 的 MAX_CANDIDATES 兜底。
    """
    texts: list[str] = []
    sessions = 0
    for log in sorted(root.glob("*/*.jsonl")):
        if detect_transcript_kind(log) != "session-log":
            continue
        texts.extend(user_texts_from_session_log(log))
        sessions += 1
        if sessions >= max_sessions:
            break
    merged = _dedupe(texts)
    candidates = scan_texts(merged, store=store)
    summary = _write_manifest(store, f"{root} (batch)", "session-log/batch", merged, candidates)
    summary["sessions"] = sessions
    return summary
