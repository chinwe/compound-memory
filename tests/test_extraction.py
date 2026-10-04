"""抽取清单扫描器的测试（issue：抽取管线 P0，确定性段、零 LLM）。

抽取是蒸馏三段式的第二应用：扫描只「发现候选」，写库仍由 Agent 走
memory_write（冲突照常入 review 队列）。这里钉住四个行为：
transcript 解析必须滤掉注入块只留真实用户话；模式匹配中英文宿主场景
的 statement/pitfall 两类；候选对库内既有条目的去重标注；清单落盘与
extract/ 目录的 gitignore 归属（运行时工件，含会话摘录，不入审计史）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from compound_memory.cli import main as cli_main
from compound_memory.extraction import (
    MAX_CANDIDATES,
    detect_transcript_kind,
    extract,
    extract_dir,
    scan_texts,
    user_texts_from_session_log,
    user_texts_from_zcode_db,
)
from compound_memory.storage import MemoryStore


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
    WorkBuddy 主源，sqlite 走 ZCode 会话库；两者都认不出时抛 ValueError
    而非静默产空清单。"""
    log = tmp_path / "sess_x.jsonl"
    log.write_text(_session_line("user", "<user_query>我用 uv 管理这个项目</user_query>"), encoding="utf-8")
    assert extract(log, store)["parser"] == "session-log"

    db = _zcode_db(tmp_path / "db.sqlite", [("user", [{"type": "text", "text": "部署在 Vercel 上，注意 10 秒超时"}])])
    assert extract(db, store)["parser"] == "zcode-db"

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
    team-lead agent 的派活文本（第三人称转述用户），实测候选 3/3 全是噪声。"""
    projects = tmp_path / "projects"
    sess_a = projects / "Users-x-workspace-a"
    sess_b = projects / "Users-y-workspace-b"
    sub = sess_a / "abc" / "subagents"
    for d in (sess_a, sess_b, sub):
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
        _session_line("user", "## 任务：构建鱼吃鱼网页游戏 MVP\n\n用户俊伟想要一个网页游戏，失败要重试"),
        encoding="utf-8",
    )
    result = extract_dir(projects, store)
    assert result["sessions"] == 2
    assert result["user_turns"] == 2, "跨会话复述应合并"
    quotes = " ".join(c["quote"] for c in json.loads(
        (store.root / "extract" / "last-candidates.json").read_text(encoding="utf-8")
    )["candidates"])
    assert "uv" in quotes and "Vercel" in quotes
    assert "鱼吃鱼" not in quotes, "subagent 派活文本不得进清单"
