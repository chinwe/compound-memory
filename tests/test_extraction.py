"""抽取清单扫描器的测试（issue：抽取管线 P0，确定性段、零 LLM）。

抽取是蒸馏三段式的第二应用：扫描只「发现候选」，写库仍由 Agent 走
memory_write（冲突照常入 review 队列）。这里钉住四个行为：
transcript 解析必须滤掉注入块只留真实用户话；模式匹配中英文宿主场景
的 statement/pitfall 两类；候选对库内既有条目的去重标注；清单落盘与
extract/ 目录的 gitignore 归属（运行时工件，含会话摘录，不入审计史）。
"""

from __future__ import annotations

import json
from pathlib import Path

from compound_memory.cli import main as cli_main
from compound_memory.extraction import (
    MAX_CANDIDATES,
    extract,
    scan_texts,
    user_texts_from_model_io,
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


def test_model_io_user_texts_filtered_and_deduped(tmp_path: Path) -> None:
    """解析契约：注入块（system-reminder 等）滤掉、跨快照重复的用户话去重、
    只剩真实用户输入——扫描器的输入质量由这一层决定。"""
    transcript = tmp_path / "model-io-sess_test.jsonl"
    transcript.write_text(
        "\n".join(
            [
                _model_io_line(
                    [
                        _text_block("<system-reminder>\n# agentsMd\n注入内容"),
                        _text_block("我用 uv 管理这个项目"),
                    ]
                ),
                _model_io_line(
                    [
                        _text_block("我用 uv 管理这个项目"),
                        _text_block("部署在 Vercel 上，注意 10 秒超时"),
                    ]
                ),
            ]
        ),
        encoding="utf-8",
    )
    texts = user_texts_from_model_io(transcript)
    assert texts == ["我用 uv 管理这个项目", "部署在 Vercel 上，注意 10 秒超时"]


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
    transcript = tmp_path / "model-io-sess_test.jsonl"
    transcript.write_text(
        _model_io_line([_text_block("我用 edge-tts 生成中文音频，晓晓语音")]),
        encoding="utf-8",
    )
    result = extract(transcript, store)
    assert result["candidates"] >= 1
    manifest = json.loads((store.root / "extract" / "last-candidates.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(transcript)
    assert manifest["candidates"][0]["suggested_type"] == "fact"
    assert "extract/" in (store.root / ".gitignore").read_text(encoding="utf-8")


def test_cli_extract_roundtrip(tmp_path: Path, capsys) -> None:
    """CLI 缝：extract 子命令产清单并打印摘要。"""
    transcript = tmp_path / "model-io-sess_test.jsonl"
    transcript.write_text(
        _model_io_line([_text_block("记住：部署窗口是周五")]),
        encoding="utf-8",
    )
    root = tmp_path / "memroot"
    assert cli_main(["--root", str(root), "extract", str(transcript)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["candidates"] >= 1
    assert (root / "extract" / "last-candidates.json").exists()
