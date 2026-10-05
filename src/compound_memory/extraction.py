"""抽取清单扫描器：会话 transcript → 记忆候选（抽取管线的确定性段，零 LLM）。

蒸馏三段式的第二应用（spec：确定性准备自动跑、判断由 Agent 完成）：
扫描只「发现候选」，写库仍由 Agent 逐条确认走 memory_write——清单是建议、
写入是动作，同 key 冲突照常进 review 队列（不静默原则在管线里不变）。

transcript 解析支持两种宿主格式，按内容形状分发（不靠文件名约定）：

1. **session log**（WorkBuddy 主源）：`<project>/<sessionId>.jsonl`，逐轮完整
   消息（type=message / role / content[].text）。真实用户话被 `<user_query>` 或
   `<session>` 包裹，同块内混着注入块（user-context / team-context / 队友消息 /
   上下文压缩摘要）——壳剥掉、注入块整块跳过。
2. **ZCode 会话库**（`~/.zcode/cli/db/db.sqlite`）：全量对话在 SQLite 里
   （message+part 表，正文在 part 的 text 块）。rollout/model-io jsonl 快照
   已退役不接——它只剩最近几个会话，只接快照会产出"看似扫过、实则只盖住
   冰山一角"的假阴性，与 trace 同等对待。
3. **Claude Code session log**（`~/.claude/projects/<项目>/<sessionId>.jsonl`）：
   真实输入 = `type=='user'` 的 message.content（字符串或 text 块）；isMeta
   （UI 回显/命令展开）、isSidechain（子 agent 转述）、tool_result 块与
   `<command-*>`/`<local-command-stdout>` 包装都不是用户话。
4. **DeepSeek Harness session**（`~/.dsh/sessions/<项目>/<会话>/session.jsonl.zstd`）：
   zstd 压缩的 JSONL（经系统 zstd CLI 解压，不为此引 C 扩展依赖）。真实输入 =
   `user/message` 且 `data.source.kind=='user'`（runtime-context 快照 / 技能
   注入 / 审批通知走别的 source.kind）；`session.origin=='subagent'` 的子会话
   是主 agent 派活文本，整场返回空。

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

import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

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
    "[Request interrupted",
    "<command-message",
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
SKIP_LIST_MAX = 50  # 清单里 skipped 明细上限：防超大目录撑爆清单，skipped_total 仍如实计数
QUOTE_CHARS = 200  # 候选摘录截断
# 去重标注阈值：查询 token 被库内条目覆盖率（containment）。不用 normalized BM25——
# 长句查询的分母惩罚使复述句也只有 ~0.12，结构性偏低；覆盖率对「复述检测」语义正确
EXTRACT_DUP_COVERAGE = 0.5
SENTENCE_SPLIT = "。！？!?；;\n"


def collect_user_texts(raw_texts: Any) -> list[str]:
    """宿主无关的公共尾巴：剥壳滤注入（_unwrap_user_text）→ 收集 → 保序去重。

    各宿主 parser 只负责把自家格式走到「原始文本块」这一步（结构性过滤是
    真差异，留在各自 parser），注入过滤与去重共用这一堵墙——原先在四个
    parser 里各复制一份。
    """
    out: list[str] = []
    for raw in raw_texts:
        cleaned = _unwrap_user_text(raw.strip())
        if cleaned:
            out.append(cleaned)
    return _dedupe(out)


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


def user_texts_from_zcode_db(path: Path) -> list[str]:
    """ZCode 会话库（SQLite）→ 真实用户话（保序去重）。

    db 是活动 ZCode 进程的 WAL 库，只读打开（mode=ro）绝不写。用户消息由
    message.data.role=='user' 定位，正文是 part 表 text 块；synthetic 与
    model-only 的块是运行时注入（todo 提醒、hook），连同 system-reminder 等
    注入标记一并滤掉——注入过滤复用 _unwrap_user_text（与 WorkBuddy 同一堵墙）。
    """
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT p.data
            FROM message m JOIN part p ON p.message_id = m.id
            WHERE json_valid(m.data) AND json_valid(p.data)
              AND json_extract(m.data, '$.role') = 'user'
              AND json_extract(p.data, '$.type') = 'text'
            ORDER BY m.session_id, m.time_created, p.sequence
            """
        ).fetchall()
    finally:
        con.close()
    raws: list[str] = []
    for (raw,) in rows:
        # json_valid + '$.type'='text' 已在 SQL 侧保证只剩合法 JSON 的 object
        # 形状行（非 object 的 json_extract 返回 NULL，被 WHERE 过滤）——这里
        # 只再做注入块的语义过滤；metadata 形状仍防御一次（宁跳过不崩扫描）
        part: Any = json.loads(raw)
        metadata = part.get("metadata")
        if part.get("synthetic") or (isinstance(metadata, dict) and metadata.get("visibility") == "model-only"):
            continue
        raws.append(part.get("text") or "")
    return collect_user_texts(raws)


def user_texts_from_claude_log(path: Path) -> list[str]:
    """Claude Code session log jsonl → 真实用户话（保序去重）。

    只取 type=='user' 的真实输入：跳过 isSidechain（子 agent 转述）与 isMeta
    （UI 回显、命令展开）；content 字符串或 text 块都过注入过滤——<command-*>
    包装、local-command-stdout、tool_result 块、"[Request interrupted]" 提示
    不是用户话。
    """
    raws: list[str] = []
    for event in _iter_json_lines(path):
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        if event.get("isSidechain") or event.get("isMeta"):
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [content] if isinstance(content, str) else content if isinstance(content, list) else []
        for block in blocks:
            if isinstance(block, str):
                raws.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                raws.append(block.get("text") or "")
    return collect_user_texts(raws)


def _zstd_decompress(path: Path) -> str:
    """zstd CLI 解压出文本（dsh 会话是 zstd 压缩 JSONL；用系统 CLI 免引 C 扩展依赖）。"""
    zstd = shutil.which("zstd")
    if zstd is None:
        raise ValueError(
            f"zstd CLI not found on PATH; it is required to read DeepSeek Harness session files: {path}"
        )
    proc = subprocess.run([zstd, "-dc", str(path)], capture_output=True, check=False)
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"zstd failed to decompress {path}: {stderr}")
    return proc.stdout.decode("utf-8", errors="replace")


def user_texts_from_dsh_session(path: Path) -> list[str]:
    """DeepSeek Harness session.jsonl.zstd → 真实用户话（保序去重）。

    真实输入 = user/message 且 data.source.kind=='user'——runtime-context
    快照、技能注入、审批通知都走别的 source.kind，结构上就能分开，不必靠
    文本模式硬猜。session.origin=='subagent' 的子会话是主 agent 派活文本
    （第三人称转述，与 WorkBuddy subagents/ 同型噪声），整场返回空。
    """
    raws: list[str] = []
    subagent = False
    for line in _zstd_decompress(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "session":
            # session 事件在文件首行，先于所有用户消息
            subagent = event.get("origin") == "subagent"
            continue
        if event.get("type") != "user/message":
            continue
        data = event.get("data")
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "user":
            continue
        content = data.get("content") if isinstance(data, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "text":
                raws.append(block.get("text") or "")
    return [] if subagent else collect_user_texts(raws)


def user_texts_from_session_log(path: Path) -> list[str]:
    """WorkBuddy session log jsonl → 真实用户话（保序去重）。

    每行一个事件，只取 type=="message" && role=="user" 的 input_text 块——
    function_call / reasoning / file-history-snapshot 等事件不是用户话。
    """
    raws: list[str] = []
    for event in _iter_json_lines(path):
        if not isinstance(event, dict) or event.get("type") != "message" or event.get("role") != "user":
            continue
        content = event.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for block in blocks:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
                raws.append(block.get("text") or "")
    return collect_user_texts(raws)


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
    candidates = store.lexical_candidates(sorted(q_tokens), {"_shared"})
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
SQLITE_MAGIC = b"SQLite format 3\x00"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"  # zstd 帧魔数（dsh 会话文件）


def _is_zcode_db(path: Path) -> bool:
    """SQLite 魔数之外再验形状：必须有 message+part 两表（ZCode 会话库形状）。"""
    con: sqlite3.Connection | None = None
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return False
    finally:
        if con is not None:
            con.close()
    return {"message", "part"} <= names


def _head_json_events(raw: bytes) -> Iterator[dict]:
    """文件头部的 jsonl 事件流（坏行/非 object 跳过）——形态探测共用。"""
    head = raw.decode("utf-8", errors="replace")
    for line in head.splitlines()[:DETECT_MAX_LINES]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _sniff_session_log(raw: bytes, path: Path) -> bool:
    return any(e.get("type") == "message" for e in _head_json_events(raw))


def _sniff_claude_log(raw: bytes, path: Path) -> bool:
    return any(
        e.get("type") == "user" and isinstance(e.get("message"), dict) for e in _head_json_events(raw)
    )


def _sniff_zcode_db(raw: bytes, path: Path) -> bool:
    return raw.startswith(SQLITE_MAGIC) and _is_zcode_db(path)


def _sniff_dsh_session(raw: bytes, path: Path) -> bool:
    return raw.startswith(ZSTD_MAGIC)


@dataclass(frozen=True)
class HostSpec:
    """一个宿主 transcript 的全套知识——parser、内容嗅探、批量 glob、提示文案。

    宿主知识的单一定义点：新增宿主 = 一个 parser 函数 + 一行 HOSTS 表，
    形态探测、目录批量、unsupported 提示、CLI 帮助全部由表驱动，勿在
    别处新增分支（2026-10-05 表驱动化前的散点教训：6 处 2 文件）。
    """

    key: str  # transcript kind（manifest parser 字段、_host_for 查找键）
    parser: Callable[[Path], list[str]]  # 宿主文件 → 原始文本块 → collect_user_texts
    sniff: Callable[[bytes, Path], bool]  # 内容形态嗅探（文件头原始字节 + 路径）
    dir_globs: tuple[str, ...]  # extract_dir 批量 glob（空 = 无目录批量形态）
    label: str  # unsupported 提示里的宿主名
    location: str  # unsupported 提示里的落点说明
    summary_en: str  # CLI --help 里的英文一句话描述


# 表序即探测优先序：二进制魔数（sqlite/zstd）先于文本行嗅探，WorkBuddy/Claude
# 各按自家事件形态判定（真实文件互不串形；理论上混合形态按表序先命中先返回）
HOSTS: tuple[HostSpec, ...] = (
    HostSpec(
        key="zcode-db",
        parser=user_texts_from_zcode_db,
        sniff=_sniff_zcode_db,
        dir_globs=(),
        label="ZCode 会话库",
        location="~/.zcode/cli/db/db.sqlite",
        summary_en="the ZCode session database (~/.zcode/cli/db/db.sqlite, full history)",
    ),
    HostSpec(
        key="dsh-session",
        parser=user_texts_from_dsh_session,
        sniff=_sniff_dsh_session,
        # dsh 会话文件有两代文件名（session.jsonl.zstd / session.v3.jsonl.zstd），
        # 事件形态相同——glob 只认旧名会静默漏掉新会话（实测 25 个里 14 个是 v3）
        dir_globs=("*/*/session*.jsonl.zstd",),
        label="DeepSeek Harness session",
        location="~/.dsh/sessions/<项目>/<会话>/session.jsonl.zstd",
        summary_en="DeepSeek Harness zstd-compressed session files (~/.dsh/sessions)",
    ),
    HostSpec(
        key="session-log",
        parser=user_texts_from_session_log,
        sniff=_sniff_session_log,
        dir_globs=("*/*.jsonl",),
        label="WorkBuddy session log",
        location="~/.workbuddy/projects/<项目>/<sessionId>.jsonl",
        summary_en="WorkBuddy session log jsonl (~/.workbuddy/projects)",
    ),
    HostSpec(
        key="claude-log",
        parser=user_texts_from_claude_log,
        sniff=_sniff_claude_log,
        dir_globs=("*/*.jsonl",),
        label="Claude Code session log",
        location="~/.claude/projects/<项目>/<sessionId>.jsonl",
        summary_en="Claude Code session log jsonl (~/.claude/projects)",
    ),
)

# 退役源说明（不随 HOSTS 变化）：trace / rollout 快照只剩部分轮次，接进来是假阴性
RETIRED_SOURCES_NOTE = (
    "WorkBuddy traces/ 与 ZCode rollout/model-io 快照不接入：都只剩部分轮次，"
    "接进来是'看似扫过、实则大面积漏'的假阴性。"
)


def _host_for(kind: str) -> HostSpec | None:
    return next((h for h in HOSTS if h.key == kind), None)


def unsupported_hint(path: Path) -> str:
    """unsupported 判定的行内理由：受支持源清单由 HOSTS 表生成（新宿主自动出现）。"""
    return (
        f"不支持的 transcript 形态：{path}。受支持的源有——"
        + "；".join(f"{h.label}（{h.location}）" for h in HOSTS)
        + "；jsonl 传目录则批量扫。"
        + RETIRED_SOURCES_NOTE
    )


def supported_hosts_summary_en() -> str:
    """CLI --help 用的英文宿主清单（同表生成，宿主增删只改表）。"""
    return "; ".join(h.summary_en for h in HOSTS)


def detect_transcript_kind(path: Path) -> str:
    """按内容形状判定 transcript 形态（HOSTS 表驱动，依表序先命中先返回）：
    session-log / zcode-db / claude-log / dsh-session，认不出为 unsupported。

    不靠文件名约定——各家都把日志叫 .jsonl / .sqlite / .zstd。也不只看第一
    行：较新会话以 session-meta / mode 等元事件开头，只看首行会把它们全判成
    unsupported 静默跳过（嗅探谓词扫文件头的前若干行）。

    SQLite 按魔数 + message/part 表形状识别；zstd 按帧魔数识别（dsh 会话）；
    model-io 快照与 trace 是 retired 源，判 unsupported——给出行内理由并指向
    受支持源，避免"看似扫过、实则大面积漏"。
    """
    with open(path, "rb") as fh:
        raw = fh.read(DETECT_HEAD_CHARS)
    for host in HOSTS:
        if host.sniff(raw, path):
            return host.key
    return "unsupported"


def extract(transcript: Path, store: MemoryStore) -> dict[str, Any]:
    """入口：单文件按内容形态分派（目录走批量扫），解析、扫描、清单落
    <root>/extract/last-candidates.json。

    extract/ 是运行时工件目录（_ensure_layout 统一 gitignore）——清单含会话
    摘录，不进记忆库的审计史；返回摘要供 CLI 打印。
    """
    if transcript.is_dir():
        return extract_dir(transcript, store)
    kind = detect_transcript_kind(transcript)
    host = _host_for(kind)
    if host is None:
        raise ValueError(unsupported_hint(transcript))
    texts = host.parser(transcript)
    candidates = scan_texts(texts, store=store)
    return _write_manifest(store, transcript, kind, texts, candidates)


def _write_manifest(
    store: MemoryStore,
    source: str | Path,
    kind: str,
    texts: list[str],
    candidates: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir = store.root / "extract"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "last-candidates.json"
    manifest: dict[str, Any] = {
        "generated": store.today(),  # clock seam：日期可测，不读墙钟
        "source": str(source),
        "parser": kind,
        "user_turns": len(texts),
        "candidates": candidates,
    }
    if extra:
        manifest.update(extra)
    out_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "candidates": len(candidates),
        "user_turns": len(texts),
        "parser": kind,
        "out": str(out_path),
        "source": str(source),
        **(extra or {}),
    }


def extract_dir(
    root: Path,
    store: MemoryStore,
    max_sessions: int = EXTRACT_MAX_SESSIONS,
) -> dict[str, Any]:
    """批量扫一个宿主日志目录。

    支持的目录布局：WorkBuddy `~/.workbuddy/projects` 与 Claude Code
    `~/.claude/projects`（都是 `<项目>/<会话>.jsonl`），DeepSeek Harness
    `~/.dsh/sessions`（`<项目>/<会话>/session*.jsonl.zstd`）。每个文件按内容
    形态分派解析器。

    只吃一级会话文件，**跳过 subagents/**：那里的 role:user 是 team-lead
    agent 的派活文本（"用户想要…" 是第三人称转述，不是本人陈述），实测 3/3
    候选全是噪声——混进来只会污染清单。dsh 的 subagent 子会话在解析器内按
    session.origin 识别并返回空（计为已扫会话）。

    **覆盖面不静默**（dsh v3 教训：glob 窄于现实时 44% 会话静默漏扫）：summary
    附 skipped 观测——目录里全部 jsonl/zstd 与已解析集合做差，按 subagents /
    unsupported-kind / outside-batch-globs 归因（明细截断到 SKIP_LIST_MAX，
    skipped_total 仍如实计数）；触达 max_sessions 上限时的余量同样计入
    outside-batch-globs。

    跨会话合并去重后再扫（同一句话在多会话复述），单会话上限由
    scan_texts 的 MAX_CANDIDATES 兜底。
    """
    paths: set[Path] = set()
    # 批量 glob 来自 HOSTS 表（dsh v3 双代文件名教训在表内注释）——新宿主自动被批量吃到
    for spec in HOSTS:
        for pattern in spec.dir_globs:
            paths.update(root.glob(pattern))
    texts: list[str] = []
    sessions = 0
    unsupported: set[Path] = set()
    subagent_files: set[Path] = set()
    for log in sorted(paths):
        if "subagents" in log.parts:
            subagent_files.add(log)
            continue
        host = _host_for(detect_transcript_kind(log))
        if host is None:
            unsupported.add(log)
            continue
        texts.extend(host.parser(log))
        sessions += 1
        if sessions >= max_sessions:
            break
    candidates_set = set(root.glob("**/*.jsonl")) | set(root.glob("**/*.zstd"))
    skipped: list[dict[str, str]] = [
        {"path": p.relative_to(root).as_posix(), "reason": "unsupported-kind"} for p in sorted(unsupported)
    ]
    for p in sorted(candidates_set - paths - unsupported - subagent_files):
        reason = "subagents" if "subagents" in p.parts else "outside-batch-globs"
        skipped.append({"path": p.relative_to(root).as_posix(), "reason": reason})
    skipped.sort(key=lambda item: item["path"])
    merged = _dedupe(texts)
    candidates = scan_texts(merged, store=store)
    summary = _write_manifest(
        store,
        f"{root} (batch)",
        "batch",
        merged,
        candidates,
        extra={"skipped": skipped[:SKIP_LIST_MAX], "skipped_total": len(skipped)},
    )
    summary["sessions"] = sessions
    return summary
