"""健康时序（#100）：stats 快照追加 health/health.jsonl + 趋势读面。

CLI-only 运维动词（health-snapshot / health-trend）的确定性段，不进 MCP 面。
单写者假设：追加不持写锁——一次定时任务或一次人工调用；同日多次调用即多行，
去重归读面（按 date 取每日最后一条，文件序即追加序）。
指标单一来源是 stats 动词：行 = date + stats dict 原样；trend 的比率与增量
一律读时现算，不落盘派生值。health/ 与 extract/ 同型——运行时工件目录
（ensure_layout 统一 gitignore 清单），不入 git、不属审计链（ADR-0006 无涉）。

新入口对表自查（身份门不变量）：snapshot/trend 不返回单条记忆正文或元数据，
产出是库级聚合计数（与 stats / doctor 同型），私有 ns 内容不可由此泄露——
trend 更是纯文件读、不开 store，无需过 _check_ns_owner/_resolve_identity 两道门。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from .scoring import age_days

# 趋势行的直读指标（stats 键原样，缺键按 0 容错——老行可能缺后来新增的键）
_DIRECT_METRICS = (
    "total",
    "avg_confidence",
    "review_queue_entries",
    "distilled_recent_7d",
    "recent_feedback_7d",
)
# 比率指标（读时由分子/total 现算）：分子取自 stats 同名直读键
_RATE_METRICS = {
    "archive_rate": "archived",
    "cross_validated_rate": "cross_validated",
}
# delta 覆盖趋势行除 date 外的全部指标（直读 + 比率，首行减尾行方向 last - first）
_DELTA_METRICS = _DIRECT_METRICS + tuple(_RATE_METRICS)


def health_path(root: Path) -> Path:
    """时序工件路径（root/health/health.jsonl）。"""
    return root / "health" / "health.jsonl"


def append_snapshot(root: Path, stats_payload: dict[str, Any], today: str) -> dict[str, Any]:
    """追加一行 {date, stats}（today 为 ISO 日期串，来自注入 clock 的公开出口）。

    单写者假设下不持写锁；调用方错误（不可写路径等）抛 OSError 由 CLI 翻译。
    """
    path = health_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"date": today, "stats": stats_payload}, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return {"ok": True, "date": today, "path": str(path), "snapshot": stats_payload}


def _trend_row(date: str, stats_payload: dict[str, Any]) -> dict[str, Any]:
    """单日趋势行：直读指标照抄 stats 键，比率指标读时现算。"""
    row: dict[str, Any] = {"date": date}
    for key in _DIRECT_METRICS:
        row[key] = stats_payload.get(key, 0)
    total = stats_payload.get("total", 0)
    for rate_key, numer_key in _RATE_METRICS.items():
        row[rate_key] = round(stats_payload.get(numer_key, 0) / total, 3) if total else 0.0
    return row


def trend(root: Path, days: int, today: dt.date) -> dict[str, Any]:
    """读最近 days 天（[today-days, today]，与 stats 近窗同策），每日取最后一条。

    坏行 fail-safe 跳过并计数（skipped_lines），不中断读面；文件缺失或窗口内
    无数据返回显式空态（status=empty + reason），退出码语义归 CLI（恒 0）。
    """
    if days < 1:
        raise ValueError(f"--days must be >= 1, got {days}")
    report: dict[str, Any] = {
        "status": "empty",
        "rows": [],
        "count": 0,
        "delta": None,
        "window_days": days,
        "skipped_lines": 0,
        "reason": "missing-file",
    }
    path = health_path(root)
    if not path.exists():
        return report
    daily: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            date = str(record["date"])
            age = age_days(date, today)
            stats_payload = record["stats"]
            if not isinstance(stats_payload, dict):
                raise TypeError("stats is not an object")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            report["skipped_lines"] += 1
            continue
        # 窗口 [today-days, today]：与 stats 近窗（recent_feedback_7d 等）同策取半开区间内年龄
        if age is None or not 0 <= age <= days:
            continue
        daily[date] = stats_payload  # 同日多行后写覆盖先写 = 每日最后一条
    if not daily:
        report["reason"] = "no-snapshots-in-window"
        return report
    rows = [_trend_row(date, daily[date]) for date in sorted(daily)]
    report["rows"] = rows
    report["count"] = len(rows)
    report["delta"] = _delta(rows[0], rows[-1])
    report["status"] = "ok"
    report.pop("reason")
    return report


def _delta(first: dict[str, Any], last: dict[str, Any]) -> dict[str, Any]:
    """首尾行各指标的 last - first（浮点差 round 3）。"""
    out: dict[str, Any] = {"from": first["date"], "to": last["date"]}
    for key in _DELTA_METRICS:
        out[key] = round(last[key] - first[key], 3)
    return out
