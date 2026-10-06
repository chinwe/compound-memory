"""抽取清单扫描器的测试（issue：抽取管线 P0，确定性段、零 LLM）。

抽取是蒸馏三段式的第二应用：扫描只「发现候选」，写库仍由 Agent 走
memory_write（冲突照常入 review 队列）。这里钉住四个行为：
transcript 解析必须滤掉注入块只留真实用户话；模式匹配中英文宿主场景
的 statement/pitfall 两类；候选对库内既有条目的去重标注；清单落盘与
extract/ 目录的 gitignore 归属（运行时工件，含会话摘录，不入审计史）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.extraction import (
    MAX_CANDIDATES,
    PITFALL_PATTERNS,
    STATEMENT_PATTERNS,
    _match_pattern,
    detect_transcript_kind,
    extract,
    extract_dir,
    scan_texts,
    user_texts_from_claude_log,
    user_texts_from_dsh_session,
    user_texts_from_session_log,
    user_texts_from_zcode_db,
)
from compound_memory.model import MEMORY_TYPES
from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE

ZSTD_AVAILABLE = shutil.which("zstd") is not None
requires_zstd = pytest.mark.skipif(not ZSTD_AVAILABLE, reason="zstd CLI not available")


def _model_io_line(user_blocks: list[dict]) -> str:
    return json.dumps(
        {
            "type": "model_io",
            "request": {
                "body": {
                    "messages": [
                        {"role": "user", "content": user_blocks},
                        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                    ]
                }
            },
        },
        ensure_ascii=False,
    )


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


# --- ZCode 宿主解析层 -------------------------------------------------------
# 真实形态（实测 ~/.zcode/cli/db/db.sqlite）：全量对话在 SQLite 库里，
# message.data.role=='user' 过滤用户消息，正文在 part 表 text 块；
# rollout/model-io jsonl 只剩最近几个会话的 API 快照，已退役不接。


def _zcode_db(path: Path, messages: list[tuple[str, list[dict | str]]]) -> Path:
    """构建最小 ZCode 会话库 fixture：message+part 两表，data 存 JSON 字符串。

    messages: (role, part payloads)——payload 是 part.data 的 JSON 字典，
    或坏 JSON 字符串（坏行容错路径）。
    """
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)")
    con.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, sequence INTEGER, data TEXT)"
    )
    for i, (role, payloads) in enumerate(messages):
        mid = f"msg_{i}"
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (mid, f"sess_{i % 2}", 1786000000000 + i, json.dumps({"role": role}, ensure_ascii=False)),
        )
        for j, payload in enumerate(payloads):
            con.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"part_{i}_{j}",
                    mid,
                    f"sess_{i % 2}",
                    1786000000000 + i,
                    j,
                    payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                ),
            )
    con.commit()
    con.close()
    return path


def test_zcode_db_user_texts_filtered_and_deduped(tmp_path: Path) -> None:
    """解析契约：synthetic / model-only 注入块滤掉、system-reminder 块滤掉、
    assistant 话不进输入、坏 JSON 行跳过、跨会话复述去重——db 是活动 ZCode
    进程的 WAL 库，解析必须只读打开（fixture 建完即关闭连接）。"""
    db = _zcode_db(
        tmp_path / "db.sqlite",
        [
            (
                "user",
                [
                    {"type": "text", "text": "<system-reminder>\n# agentsMd\n注入内容"},
                    {"type": "text", "text": "我用 uv 管理这个项目"},
                ],
            ),
            ("assistant", [{"type": "text", "text": "记住了"}]),
            (
                "user",
                [
                    {"type": "text", "text": "todo 提醒", "synthetic": True},
                    {"type": "text", "text": "hook 注入", "metadata": {"visibility": "model-only"}},
                ],
            ),
            ("user", ["{ broken json"]),
            ("user", [{"type": "text", "text": "部署在 Vercel 上，注意 10 秒超时"}]),
            ("user", [{"type": "text", "text": "我用 uv 管理这个项目"}]),
        ],
    )
    assert user_texts_from_zcode_db(db) == ["我用 uv 管理这个项目", "部署在 Vercel 上，注意 10 秒超时"]


# --- Claude Code 宿主解析层 --------------------------------------------------
# 真实形态（实测 ~/.claude/projects/<slug>/<uuid>.jsonl）：首行是 mode /
# permission-mode / file-history-snapshot 等元事件；真实输入 = type=='user' 的
# message.content（字符串或 text 块），isMeta 是 UI 回显/命令展开、isSidechain
# 是子 agent 转述、tool_result 块与 <command-*>/<local-command-stdout> 包装、
# "[Request interrupted by user]" 系统提示都不是用户话。


def _claude_user(content: str | list[dict], *, is_meta: bool = False, sidechain: bool = False) -> str:
    event: dict = {
        "parentUuid": None,
        "isSidechain": sidechain,
        "type": "user",
        "message": {"role": "user", "content": content},
        "uuid": "u",
        "sessionId": "s",
    }
    if is_meta:
        event["isMeta"] = True
    return json.dumps(event, ensure_ascii=False)


def test_claude_log_user_texts_filtered_and_deduped(tmp_path: Path) -> None:
    """解析契约：isMeta / isSidechain / tool_result / 命令包装 / 系统提示全滤掉，
    字符串与 text 块两种 content 形态都收，跨行重复去重。"""
    log = tmp_path / "sess_test.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"type": "mode", "mode": "normal", "sessionId": "s"}),
                _claude_user("我用 uv 管理这个项目"),
                _claude_user("<system-reminder>\n# agentsMd\n注入内容"),
                _claude_user("<command-message>doctor</command-message>\n<command-name>/doctor</command-name>"),
                _claude_user("<local-command-stdout>Bye!</local-command-stdout>"),
                _claude_user("[glm-5.2] ░░░░░░░░░░ 0% | project", is_meta=True),
                _claude_user([{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]),
                _claude_user([{"type": "text", "text": "[Request interrupted by user]"}]),
                _claude_user([{"type": "text", "text": "部署在 Vercel 上，注意 10 秒超时"}]),
                _claude_user("我用 uv 管理这个项目", sidechain=True),
                json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "ok"}}),
                _claude_user("我用 uv 管理这个项目"),
            ]
        ),
        encoding="utf-8",
    )
    assert user_texts_from_claude_log(log) == ["我用 uv 管理这个项目", "部署在 Vercel 上，注意 10 秒超时"]


# --- DeepSeek Harness 宿主解析层 ---------------------------------------------
# 真实形态（实测 ~/.dsh/sessions/<slug>/<会话>/session.jsonl.zstd）：文件是
# zstd 压缩的 JSONL；真实输入 = user/message 且 data.source.kind=='user'
# （runtime-context 快照 / 技能注入 / 审批通知走别的 source.kind）；session
# 事件 origin=='subagent' 的是主 agent 派生的子会话（第三人称派活文本）。


def _dsh_user_message(text: str, *, source_kind: str | None = "user") -> str:
    data: dict = {"content": [{"type": "text", "text": text}], "role": "user"}
    if source_kind is not None:
        data["source"] = {"kind": source_kind}
    return json.dumps({"type": "user/message", "seq": 0, "time": 0, "data": data}, ensure_ascii=False)


def _write_dsh_session(tmp_path: Path, lines: list[str]) -> Path:
    src = tmp_path / "session.jsonl"
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dest = tmp_path / "session.jsonl.zstd"
    subprocess.run(["zstd", "-f", "-q", "-o", str(dest), str(src)], check=True)
    return dest


@requires_zstd
def test_dsh_session_user_texts_filtered_and_deduped(tmp_path: Path) -> None:
    """解析契约：source.kind=='user' 才是真人输入（plugin 通知、无 source 的
    runtime 快照、system-reminder 技能注入全滤），坏行跳过、重复去重。"""
    dsh = _write_dsh_session(
        tmp_path,
        [
            json.dumps({"type": "session", "version": 0, "id": "s", "createdAt": 0, "cwd": "/tmp"}),
            _dsh_user_message("我用 uv 管理这个项目"),
            _dsh_user_message('The approval policy changed from "ask" to "never".', source_kind="plugin"),
            _dsh_user_message("Current runtime context. This snapshot supersedes earlier runtime-context snapshots.", source_kind=None),
            _dsh_user_message("<system-reminder>\nA skill is a reusable set of task-specific instructions."),
            _dsh_user_message("部署在 Vercel 上，注意 10 秒超时"),
            "{ broken json",
            _dsh_user_message("我用 uv 管理这个项目"),
        ],
    )
    assert user_texts_from_dsh_session(dsh) == ["我用 uv 管理这个项目", "部署在 Vercel 上，注意 10 秒超时"]


@requires_zstd
def test_dsh_subagent_session_yields_nothing(tmp_path: Path) -> None:
    """session.origin=='subagent' 的子会话是主 agent 的派活文本（第三人称转述，
    与 WorkBuddy subagents/ 同型噪声），整场返回空——宁可不扫，不可污染清单。"""
    dsh = _write_dsh_session(
        tmp_path,
        [
            json.dumps(
                {"type": "session", "version": 0, "id": "s", "createdAt": 0, "cwd": "/tmp", "origin": "subagent"}
            ),
            _dsh_user_message("用户想要一个宣传海报，请分析图片"),
        ],
    )
    assert user_texts_from_dsh_session(dsh) == []


@requires_zstd
def test_extract_dir_covers_dsh_v3_files(tmp_path: Path, store: MemoryStore) -> None:
    """回归：dsh 会话文件有两代文件名（session.jsonl.zstd / session.v3.jsonl.zstd），
    事件形态相同。批量 glob 若只认旧名会静默漏掉一半会话（实测 25 个里 14 个是
    v3）——WorkBuddy「session-meta 开头」教训的同型坑。"""
    root = tmp_path / "sessions"
    for name in ("session.jsonl.zstd", "session.v3.jsonl.zstd"):
        d = root / "proj-x" / f"sid-{name.split('.')[1]}"
        d.mkdir(parents=True)
        src = d / "tmp.jsonl"
        src.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "version": 0, "id": "s", "createdAt": 0, "cwd": "/tmp"}),
                    _dsh_user_message("记住：部署窗口是周五"),
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["zstd", "-f", "-q", "-o", str(d / name), str(src)], check=True)
    result = extract_dir(root, store)
    assert result["sessions"] == 2, "两代文件名都必须被批量扫描吃到"
    assert result["candidates"] >= 1


def test_scan_matches_statement_and_pitfall_skips_noise() -> None:
    """两类模式各自命中（statement → fact、pitfall → insight）、无关句不进清单；
    signal 记录命中的模式词，quote 保留原句供 Agent 判断。"""
    cands = scan_texts(
        [
            "我用 uv 管理这个项目",
            "今天天气不错",
            "不要用 pytest 内置 tmp_path，沙箱有坑",
        ]
    )
    assert [c["suggested_type"] for c in cands] == ["fact", "insight"]
    assert "我用" in cands[0]["signal"] or "我用的是" in cands[0]["signal"]
    assert "不要用" in cands[1]["signal"]
    assert cands[0]["quote"].startswith("我用 uv")
    assert "天气" not in "".join(c["quote"] for c in cands)


def test_scan_caps_candidates() -> None:
    """上限截断：喋喋不休的会话不产生无限清单。"""
    texts = [f"我用工具 {i} 完成部署" for i in range(MAX_CANDIDATES + 10)]
    assert len(scan_texts(texts)) == MAX_CANDIDATES


def test_suggested_type_labels_are_backed_by_type_spec() -> None:
    """#50 附注防线：suggested_type 标签绝不能跑在 TYPE_SPEC 前面——
    建议值没有类型背书时，跟随建议的 memory_write 会直接 ValueError。
    对全部模式逐个探测，任何模式加表而类型缺背书即在此红。"""
    probes = [f"{p} something" for p in STATEMENT_PATTERNS] + [f"{p} something" for p in PITFALL_PATTERNS]
    labels = {_match_pattern(s)[0] for s in probes}
    assert labels, "pattern table must yield at least one suggested label"
    assert labels <= set(MEMORY_TYPES)


def test_scan_dedup_marks_existing_memory(store: MemoryStore) -> None:
    """库内已有近似条目（高词面重叠的复述）⇒ 候选标 likely_dup_of（指向既有 id），
    让 Agent 复用同 key 而非新开条目（spec 写入约定）。
    度量是查询 token 覆盖率（containment）——normalized BM25 对长句查询结构性偏低
    （实测复述句仅 0.12）；漏标由 Agent 自行 search 兜底，扫描器不静默丢候选。"""
    store.write(content="项目用 uv 管理，日常一律 uv run", type="fact", source="agent-zcode", key="proj-tooling")
    cands = scan_texts(["记住：项目用 uv 管理，日常一律 uv run"], store=store)
    assert cands, "相似句应仍进清单（由 Agent 判断，不是扫描器静默丢弃）"
    assert cands[0]["likely_dup_of"], "高词面重叠候选必须带既有条目标注"


def test_extract_writes_manifest_and_gitignores_dir(store: MemoryStore, tmp_path: Path) -> None:
    """清单落 <root>/extract/last-candidates.json（含溯源与候选），
    extract/ 由 _ensure_layout 统一进 .gitignore（含会话摘录，不入审计史）。"""
    db = _zcode_db(tmp_path / "db.sqlite", [("user", [{"type": "text", "text": "我用 edge-tts 生成中文音频，晓晓语音"}])])
    result = extract(db, store)
    assert result["candidates"] >= 1
    manifest = json.loads((store.root / "extract" / "last-candidates.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(db)
    assert manifest["candidates"][0]["suggested_type"] == "fact"
    assert "extract/" in (store.root / ".gitignore").read_text(encoding="utf-8")


def test_cli_extract_roundtrip(tmp_path: Path, capsys) -> None:
    """CLI 缝：extract 子命令产清单并打印摘要。"""
    db = _zcode_db(tmp_path / "db.sqlite", [("user", [{"type": "text", "text": "记住：部署窗口是周五"}])])
    root = tmp_path / "memroot"
    assert cli_main(["--root", str(root), "extract", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["candidates"] >= 1
    assert (root / "extract" / "last-candidates.json").exists()


# --- WorkBuddy 宿主解析层 -------------------------------------------------
# 真实形态（实测 ~/.workbuddy）：对话正文有两个源，形态完全不同——
#   1. session log：<project>/<sessionId>.jsonl，完整逐轮消息，user text 被
#      <user_query> / <session> 包裹，同块内还混着 system-reminder 注入。
#   2. trace：traces/<pid>/trace_*.json 里 name=="generation" 的 span，
#      toolInput 是被截到 100k 的请求快照（整段 json.loads 会抛错），需逐条
#      raw_decode 抢救 —— 但抢救只拿得到每 span 的第一条 user 消息（头部截断，
#      后续轮次已被切掉），因此 trace 只是降级兜底，session log 才是主源。


def _session_line(role: str, text: str) -> str:
    block_type = "input_text" if role == "user" else "output_text"
    return json.dumps(
        {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
        ensure_ascii=False,
    )


def test_session_log_unwraps_and_filters(tmp_path: Path) -> None:
    """session log 解析契约：<user_query> / <session> 壳剥掉、注入块滤掉、
    assistant 话不进输入、坏行跳过。"""
    log = tmp_path / "sess_test.jsonl"
    log.write_text(
        "\n".join(
            [
                _session_line("user", '<user_query>我用 uv 管理这个项目</user_query>'),
                _session_line("assistant", "记住了"),
                _session_line("user", "<session>\n记忆库\n</session>"),
                _session_line("user", '<system-reminder data-role="user-context">\n<user_info>\nOS</user_info>'),
                _session_line("user", '<teammate-message teammate_id="lead">\n任务分配</teammate-message>'),
                _session_line(
                    "user",
                    "<user_query>我用 uv 管理这个项目</user_query>",
                ),  # 跨轮复述去重
                "{ this is not json",
                _session_line("user", "<user_query>部署在 Vercel，注意 10 秒超时</user_query>"),
            ]
        ),
        encoding="utf-8",
    )
    assert user_texts_from_session_log(log) == [
        "我用 uv 管理这个项目",
        "记忆库",
        "部署在 Vercel，注意 10 秒超时",
    ]


def test_session_log_skips_history_summary_blocks(tmp_path: Path) -> None:
    """上下文压缩产物不是用户话：<conversation_history_summary> 与
    "Please continue with the conversation..." 续写指令整块跳过。"""
    log = tmp_path / "sess_sum.jsonl"
    log.write_text(
        "\n".join(
            [
                _session_line("user", "<conversation_history_summary>\nSummary of ...</conversation_history_summary>"),
                _session_line(
                    "user",
                    "Please continue with the conversation based on the summarized context above. Maintain the "
                    "same level of detail",
                ),
                _session_line("user", "<user_query>记得我偏好中文</user_query>"),
            ]
        ),
        encoding="utf-8",
    )
    assert user_texts_from_session_log(log) == ["记得我偏好中文"]


def test_detect_handles_leading_session_meta_lines(tmp_path: Path) -> None:
    """回归：较新会话以 session-meta 事件开头（不是 message），只探首行会把它
    误判成 unknown —— 实测 92 个真实会话里 16 个（含当前会话）被静默跳过，
    且恰好是最近的会话。探测必须扫前若干行找 message，不能只看第一行。"""
    log = tmp_path / "sess_meta_first.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"type": "session-meta", "id": "a", "meta": {"codebuddy.ai/hostKind": "unopted"}}),
                json.dumps({"type": "session-meta", "id": "b", "meta": {}}),
                _session_line("user", "<user_query>我默认用 uv</user_query>"),
            ]
        ),
        encoding="utf-8",
    )
    assert detect_transcript_kind(log) == "session-log"
    assert user_texts_from_session_log(log) == ["我默认用 uv"]


def test_detect_zcode_db_retired_model_io_and_trace(tmp_path: Path, store: MemoryStore) -> None:
    """形态判别：ZCode 会话库按 SQLite 魔数 + message/part 表判定；
    **model-io jsonl 已退役**——rollout/ 只剩最近几个会话的 API 快照，
    db 才是全量主源，接快照会产出"看起来扫过、实际只盖住冰山一角"的假阴性；
    **trace 同样不是受支持的源**（generation span toolInput 被头部硬截到
    100000 字符，单快照只剩首轮 user 消息）。退役源一律判 unsupported 并
    指向受支持源，而不是静默扫个残缺。"""
    db = _zcode_db(tmp_path / "db.sqlite", [("user", [{"type": "text", "text": "我用 uv 管理这个项目"}])])
    assert detect_transcript_kind(db) == "zcode-db"

    # 有 SQLite 魔数但不是 ZCode 会话库形状（缺 message/part 表）→ unsupported
    other_sqlite = tmp_path / "other.sqlite"
    con = sqlite3.connect(other_sqlite)
    con.execute("CREATE TABLE t (x TEXT)")
    con.commit()
    con.close()
    assert detect_transcript_kind(other_sqlite) == "unsupported"

    claude_log = tmp_path / "c.jsonl"
    claude_log.write_text(
        "\n".join(
            [
                json.dumps({"type": "mode", "mode": "normal", "sessionId": "s"}),
                _claude_user("我用 uv 管理这个项目"),
            ]
        ),
        encoding="utf-8",
    )
    assert detect_transcript_kind(claude_log) == "claude-log"

    if ZSTD_AVAILABLE:
        dsh = _write_dsh_session(
            tmp_path,
            [json.dumps({"type": "session", "version": 0, "id": "s", "createdAt": 0, "cwd": "/tmp"})],
        )
        assert detect_transcript_kind(dsh) == "dsh-session"

    model_io = tmp_path / "m.jsonl"
    model_io.write_text(_model_io_line([_text_block("我用的是 uv")]), encoding="utf-8")
    assert detect_transcript_kind(model_io) == "unsupported"

    trace = tmp_path / "trace_x.json"
    trace.write_text(
        json.dumps(
            {
                "trace": {"sessionId": "s"},
                "spans": [
                    {
                        "name": "generation",
                        "toolInput": json.dumps(
                            [{"role": "user", "content": [{"type": "text", "text": "我默认用 uv"}]}],
                            ensure_ascii=False,
                        ),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert detect_transcript_kind(trace) == "unsupported"
    try:
        extract(trace, store)
    except ValueError as exc:
        assert "session log" in str(exc), "报错要指向受支持的主源，别让用户猜"
    else:
        raise AssertionError("trace 不应被当作受支持的 transcript 源")


def test_extract_dispatches_by_transcript_shape(tmp_path: Path, store: MemoryStore) -> None:
    """extract 入口按内容形状分发解析器（不靠文件名约定）：session log 走
    WorkBuddy 主源，sqlite 走 ZCode 会话库，Claude/dsh 各走自家形态；
    都认不出时抛 ValueError 而非静默产空清单。"""
    log = tmp_path / "sess_x.jsonl"
    log.write_text(_session_line("user", "<user_query>我用 uv 管理这个项目</user_query>"), encoding="utf-8")
    assert extract(log, store)["parser"] == "session-log"

    db = _zcode_db(tmp_path / "db.sqlite", [("user", [{"type": "text", "text": "部署在 Vercel 上，注意 10 秒超时"}])])
    assert extract(db, store)["parser"] == "zcode-db"

    claude_log = tmp_path / "c_x.jsonl"
    claude_log.write_text(_claude_user("记住：部署窗口是周五"), encoding="utf-8")
    assert extract(claude_log, store)["parser"] == "claude-log"

    junk = tmp_path / "junk.txt"
    junk.write_text("完全不是 transcript", encoding="utf-8")
    try:
        extract(junk, store)
    except ValueError as exc:
        assert "transcript" in str(exc)
    else:
        raise AssertionError("无法识别的 transcript 应抛 ValueError，不静默产空清单")


def test_extract_dir_batches_and_skips_subagents(tmp_path: Path, store: MemoryStore) -> None:
    """批量模式：跨会话合并去重；**跳过 subagents/**——那里的 role:user 是
    team-lead agent 的派活文本（第三人称转述用户），实测候选 3/3 全是噪声。
    目录里混着其他宿主的会话（Claude Code 同为 <slug>/<file>.jsonl 布局）
    也一并按各自形态解析。**覆盖面不静默**：glob 没吃到的文件（dsh v3 教训
    ——glob 窄于现实时 44% 会话静默漏扫）与不支持形态的文件都要在 skipped
    里如实现身并归因。"""
    projects = tmp_path / "projects"
    sess_a = projects / "Users-x-workspace-a"
    sess_b = projects / "Users-y-workspace-b"
    sub = sess_a / "abc" / "subagents"
    deep = sess_a / "deep"
    for d in (sess_a, sess_b, sub, deep):
        d.mkdir(parents=True)
    (sess_a / "s1.jsonl").write_text(
        _session_line("user", "<user_query>我用 uv 管理这个项目</user_query>"), encoding="utf-8"
    )
    # 跨会话复述同一句 → 合并后只留一条，但仍是 1 个候选
    (sess_b / "s2.jsonl").write_text(
        "\n".join(
            [
                _session_line("user", "<user_query>我用 uv 管理这个项目</user_query>"),
                _session_line("user", "<user_query>部署在 Vercel，注意 10 秒超时</user_query>"),
            ]
        ),
        encoding="utf-8",
    )
    # subagent 派活文本：含模式词但不是本人陈述
    (sub / "agent-x.jsonl").write_text(
        _session_line("user", "## 任务：构建鱼吃鱼网页游戏 MVP\n\n用户想要一个网页游戏，失败要重试"),
        encoding="utf-8",
    )
    # 混入一个 Claude Code 会话文件：按内容判别后走 claude-log 解析
    (sess_b / "c3.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"type": "mode", "mode": "normal", "sessionId": "s"}),
                _claude_user("记住：部署窗口是周五"),
            ]
        ),
        encoding="utf-8",
    )
    # 深于批量 glob 的会话文件 + 不支持形态的文件：不解析，但必须在 skipped 里现身
    (deep / "nested.jsonl").write_text(
        _session_line("user", "<user_query>我用 edge-tts 生成中文音频，晓晓语音</user_query>"), encoding="utf-8"
    )
    (sess_a / "junk.jsonl").write_text("完全不是 transcript", encoding="utf-8")
    result = extract_dir(projects, store)
    assert result["sessions"] == 3
    assert result["user_turns"] == 3, "跨会话复述应合并，不同句子各留一条"
    skipped = {s["path"]: s["reason"] for s in result["skipped"]}
    assert skipped["Users-x-workspace-a/abc/subagents/agent-x.jsonl"] == "subagents"
    assert skipped["Users-x-workspace-a/deep/nested.jsonl"] == "outside-batch-globs"
    assert skipped["Users-x-workspace-a/junk.jsonl"] == "unsupported-kind"
    assert result["skipped_total"] == len(result["skipped"])
    quotes = " ".join(c["quote"] for c in json.loads(
        (store.root / "extract" / "last-candidates.json").read_text(encoding="utf-8")
    )["candidates"])
    assert "uv" in quotes and "Vercel" in quotes
    assert "鱼吃鱼" not in quotes, "subagent 派活文本不得进清单"
    assert "edge-tts" not in quotes, "skipped 文件不得被解析进清单"


def test_manifest_date_uses_store_clock(store: MemoryStore, tmp_path: Path) -> None:
    """清单 generated 日期走 store 注入的 clock（本 fixture 固定 2026-10-01）——
    不读墙钟，clock seam 全域一致、日期可测。"""
    db = _zcode_db(tmp_path / "db-clock.sqlite", [("user", [{"type": "text", "text": "我用 edge-tts 生成中文音频，晓晓语音"}])])
    extract(db, store)
    manifest = json.loads((store.root / "extract" / "last-candidates.json").read_text(encoding="utf-8"))
    assert manifest["generated"] == CLOCK_DATE.isoformat()


def test_fifth_host_is_one_table_row(tmp_path: Path, store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主知识单点 locality（回归钉）：新增宿主 = 一个 parser 函数 + 一行 HOSTS 表——
    形态探测、批量 glob、unsupported 提示、单文件/目录入口全由表驱动，无需改任何
    分支。假宿主用 FAKE_MAGIC 文件头 + 自家 glob 注入，端到端验证一次接入。"""
    from compound_memory import extraction

    def _fake_parser(path: Path) -> list[str]:
        return ["记住：假宿主的部署窗口是周五"]

    fake = extraction.HostSpec(
        key="fake-host",
        parser=_fake_parser,
        sniff=lambda raw, path: raw.startswith(b"FAKE_MAGIC"),
        dir_globs=("*/*.flog",),
        label="Fake host session log",
        location="~/fake/<项目>/<会话>.flog",
        summary_en="Fake host session files",
    )
    monkeypatch.setattr(extraction, "HOSTS", [*extraction.HOSTS, fake])

    flog = tmp_path / "proj-fake" / "s.flog"
    flog.parent.mkdir()
    flog.write_bytes(b"FAKE_MAGIC payload")
    assert extraction.detect_transcript_kind(flog) == "fake-host"
    assert extraction.extract(flog, store)["parser"] == "fake-host"
    assert extraction.extract(tmp_path, store)["sessions"] == 1, "目录批量入口的 glob 也来自表"
    junk = tmp_path / "unknown.bin"
    junk.write_bytes(b"neither magic nor jsonl")
    try:
        extraction.extract(junk, store)
    except ValueError as exc:
        assert "Fake host" in str(exc), "unsupported 提示由表生成，新宿主自动出现"
    else:
        raise AssertionError("unknown 形态应抛 ValueError")
