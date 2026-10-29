"""CLI：不经 MCP 直接访问同一 store（脚本、定时蒸馏、衰减扫描等运维面）。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .embedding import auto_encoder
from .extraction import extract, supported_hosts_summary_en
from .health import append_snapshot, trend
from .index import atomic_write_text
from .storage import DISTILL_DUP_SIM_THRESHOLD, MEMORY_TYPES, MemoryStore, PROMOTION_USES_THRESHOLD, default_root
from .storage.dream import dream_proposals_path, last_plan_path
from .storage.distill import SUGGESTED_ACTIONS
from .storage.gitlayer import probe_repo
from .storage.locking import WriteLockError, probe_write_lock
from .storage.validation import attestation_status, normalize_agent_id


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _agent_id_from_env() -> str | None:
    """进程身份只在 adapter 读环境变量（store 不读）。空白视为未设置。"""
    return normalize_agent_id(os.environ.get("COMPOUND_MEMORY_AGENT_ID"))


def _open_store(args: argparse.Namespace) -> MemoryStore:
    # CLI 与 server 同权：vec extra + 模型就绪即启用向量路，否则自动降级纯词面；
    # 环境注入的进程身份（若有）同样生效——未设置时保持自报身份模式
    return MemoryStore(
        Path(args.root),
        embedder=auto_encoder(),
        agent_id=_agent_id_from_env(),
    )


def _note_attestation(attestation: dict[str, Any]) -> None:
    warning = attestation.get("warning")
    if warning:
        print(warning, file=sys.stderr)


def _note_retrieval(retrieval: dict[str, Any]) -> None:
    if retrieval.get("mode") == "lexical":
        print(f"retrieval is lexical-only ({retrieval.get('reason')})", file=sys.stderr)


def _caller_project(args: argparse.Namespace) -> str | None:
    """调用方 project 上下文（ADR 0010）：显式 --project 优先，COMPOUND_MEMORY_PROJECT
    作 CLI 回退便利通道。env 只在 adapter 层读，store 自身不读环境变量——与
    COMPOUND_MEMORY_AGENT_ID 同型，保测试与库调用的确定性。"""
    return args.project or os.environ.get("COMPOUND_MEMORY_PROJECT") or None


def cmd_init(args: argparse.Namespace) -> None:
    store = _open_store(args)
    _emit({"ok": True, "root": str(store.root)})


def cmd_write(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).write(
            content=args.content,
            type=args.type,
            source=args.source,
            ns=args.ns,
            key=args.key,
            valid_from=args.valid_from,
            valid_until=args.valid_until,
            project=_caller_project(args),
            lesson_role=args.lesson_role,
        )
    )


def cmd_search(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).search(
            query=args.query,
            ns=args.ns,
            top_k=args.top_k,
            include_neighbors=args.include_neighbors,
            reader=args.reader,
            project=_caller_project(args),
            explain=args.explain,
        )
    )


def cmd_get(args: argparse.Namespace) -> None:
    _emit(_open_store(args).get(args.id, reader=args.reader, project=_caller_project(args)))


def cmd_explain(args: argparse.Namespace) -> None:
    _emit(_open_store(args).explain(args.id, reader=args.reader))


def cmd_link(args: argparse.Namespace) -> None:
    _emit(_open_store(args).link(args.a, args.b, agent=args.agent))


def cmd_feedback(args: argparse.Namespace) -> None:
    _emit(_open_store(args).feedback(args.id, args.agent, outcome=args.outcome))


def cmd_decay(args: argparse.Namespace) -> None:
    # --now 经固定 clock 的 store 注入——时间接缝只有 clock 一条（store 不另设 now= 参数）
    if args.now:
        store = MemoryStore(Path(args.root), clock=lambda: dt.date.fromisoformat(args.now))
    else:
        store = _open_store(args)
    _emit({"archived": store.decay_sweep()})


def cmd_revive(args: argparse.Namespace) -> None:
    _emit(_open_store(args).revive(args.id, reader=args.reader))


def cmd_forget(args: argparse.Namespace) -> None:
    _emit(_open_store(args).forget(args.id, args.agent, reason=args.reason))


def cmd_stats(args: argparse.Namespace) -> int:
    payload = _open_store(args).stats()
    # 诊断走 stderr，stdout 保持纯 JSON。未注入身份 / 词面降级在这里响一声（#57/#60）。
    # 未注入是共享部署的硬失败：JSON 照打，退出码 1。MCP 工具不走这条路径。
    attestation = payload.get("attestation") or {}
    _note_attestation(attestation)
    _note_retrieval(payload.get("retrieval") or {})
    _emit(payload)
    if not attestation.get("attested"):
        return 1
    return 0


def _skip_git(reason: str) -> dict[str, Any]:
    return {"ok": True, "repo": None, "dirty": None, "reason": reason, "skipped": True}


def _skip_lock(detail: str) -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "detail": detail, "skipped": True}


def cmd_doctor(args: argparse.Namespace) -> int:
    """一页健康诊断。硬失败退出码 1；词面降级与脏工作树只报告。

    git 快照在 MemoryStore 构造之前：构造会收编孤儿变更。根目录缺失、
    不是目录、不是 git 仓库、或写锁试加失败时不打开 store——避免 doctor
    顺手 git init，也避免锁被占用时阻塞 flock 把健康检查挂死。
    """
    root = Path(args.root)
    attestation = attestation_status(_agent_id_from_env())
    if not root.exists():
        root_info: dict[str, Any] = {"ok": False, "status": "missing", "path": str(root)}
        git_info: dict[str, Any] = _skip_git("skipped: root-missing")
        write_lock: dict[str, Any] = _skip_lock("root-missing")
        retrieval: dict[str, Any] = {"mode": None, "reason": "skipped: root-missing"}
    elif not root.is_dir():
        root_info = {"ok": False, "status": "not-a-directory", "path": str(root)}
        git_info = _skip_git("skipped: root-not-a-directory")
        write_lock = _skip_lock("root-not-a-directory")
        retrieval = {"mode": None, "reason": "skipped: root-not-a-directory"}
    else:
        root_info = {"ok": True, "status": "present", "path": str(root)}
        git_info = probe_repo(root)
        write_lock = probe_write_lock(root)
        # 写锁失败时不要打开 store：recover 的阻塞 flock 会把 doctor 挂住。
        if not git_info.get("repo"):
            retrieval = {"mode": None, "reason": "skipped: not-a-repository"}
        elif not write_lock.get("ok"):
            retrieval = {"mode": None, "reason": "skipped: write-lock"}
        else:
            retrieval = _open_store(args).retrieval_status()

    problems: list[str] = []
    if not root_info["ok"]:
        problems.append("root")
    if not attestation.get("attested"):
        problems.append("attestation")
    if not write_lock.get("skipped") and not write_lock.get("ok"):
        problems.append("write_lock")
    if not git_info.get("skipped") and not git_info.get("ok"):
        problems.append("git")

    report = {
        "ok": not problems,
        "problems": problems,
        "root": root_info,
        "attestation": attestation,
        "retrieval": retrieval,
        "write_lock": write_lock,
        "git": git_info,
    }
    _note_attestation(attestation)
    _note_retrieval(retrieval)
    if not root_info["ok"]:
        print(f"store root {root_info['status']}: {root_info['path']}", file=sys.stderr)
    if git_info.get("dirty"):
        print("git worktree is dirty", file=sys.stderr)
    elif not git_info.get("skipped") and not git_info.get("ok"):
        print(f"git: {git_info.get('reason')}", file=sys.stderr)
    if not write_lock.get("skipped") and not write_lock.get("ok"):
        print(f"write lock: {write_lock.get('detail') or write_lock.get('status')}", file=sys.stderr)
    if report["ok"]:
        print("doctor: ok", file=sys.stderr)
    else:
        print("doctor: fail (" + ", ".join(problems) + ")", file=sys.stderr)
    _emit(report)
    return 0 if report["ok"] else 1


def cmd_rebuild_index(args: argparse.Namespace) -> None:
    _emit(_open_store(args).rebuild_index())


def cmd_review_queue(args: argparse.Namespace) -> None:
    _emit(_open_store(args).review_queue())


def cmd_review_resolve(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).review_resolve(ids=args.ids, all=args.all, reader=args.reader, uphold=args.uphold)
    )


def cmd_distill_plan(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).distill_plan(
            window_days=args.window,
            min_uses=args.min_uses,
            min_confidence=args.min_confidence,
            ns=args.ns,
            reader=args.reader,
        )
    )


def cmd_dream(args: argparse.Namespace) -> None:
    # 空跑：stdout 是报告 JSON，一行摘要走 stderr（与 stats 的诊断分流同型），
    # 定时脚本重定向 stdout 时不会把摘要写进报告文件。
    # --propose 另写 gitignore 的候选包文件，不改变 stdout 的报告形状，也不 apply。
    store = _open_store(args)
    report = store.dream(
        window_days=args.window,
        min_uses=args.min_uses,
        min_confidence=args.min_confidence,
        ns=args.ns,
        reader=args.reader,
    )
    print(report["summary"], file=sys.stderr)
    if args.propose:
        pack = store.dream_proposals(ns=args.ns, reader=args.reader)
        path = dream_proposals_path(store.root)
        atomic_write_text(path, json.dumps(pack, ensure_ascii=False, indent=2) + "\n")
        empty = " empty" if not pack else ""
        print(
            f"dream proposals ns={report['ns']}: count={len(pack)}{empty} path={path}",
            file=sys.stderr,
        )
    _emit(report)


def _load_proposals(path: Path) -> list[Any]:
    """dream-apply 的产物包。必须是 JSON 列表；缺文件 / 坏 JSON 是调用方错误。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read dream-apply proposals: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"dream-apply proposals must be JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("dream-apply proposals must be a JSON list")
    return data


def cmd_dream_apply(args: argparse.Namespace) -> None:
    # 显式写入口。stdout 是计划/结果 JSON，一行计数摘要走 stderr。
    # --dry-run 不调用 distill_apply。
    report = _open_store(args).dream_apply(
        actions=_load_proposals(Path(args.proposals)),
        source=args.source,
        window_days=args.window,
        min_uses=args.min_uses,
        min_confidence=args.min_confidence,
        ns=args.ns,
        reader=args.reader,
        dry_run=args.dry_run,
    )
    print(report["summary"], file=sys.stderr)
    _emit(report)


def cmd_distill_apply(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).distill_apply(
            content=args.content,
            type=args.type,
            source=args.source,
            source_ids=[s.strip() for s in args.sources.split(",") if s.strip()],
            ns=args.ns,
            key=args.key,
            confidence=args.confidence,
        )
    )


def cmd_extract(args: argparse.Namespace) -> None:
    # 单文件/目录分派在 extraction.extract 内（按内容形态与目录批量统一收口）
    _emit(extract(Path(args.transcript), _open_store(args)))


def _filter_log_lines(lines: list[str], grep: list[str], exclude: list[str]) -> list[str]:
    """git-log 呈现层过滤（#42 / #31 消费端降噪）：--grep 正选、--exclude 反选。

    匹配原语统一 Python re.search（substring/regex，大小写敏感），对齐
    git log --grep 的消息匹配本质；git 原生 --exclude 是 ref/pathspec 排除、
    对 commit message 不生效（2.37 实测），故不透传 git 参数——避免 BRE
    与 Python re 两套正则方言在同一命令里漂移。多 --grep 是 OR（对齐
    git）；--exclude 任一命中即剔除；匹配对象是剥掉 oneline hash 前缀后的
    消息段；过滤发生在 --limit 取数之后（审计窗口内过滤）。
    """
    try:
        grep_patterns = [re.compile(p) for p in grep]
        exclude_patterns = [re.compile(p) for p in exclude]
    except re.error as exc:
        # 非法正则属调用方错误：走 CLI 统一翻译（stderr JSON + exit 2）
        raise ValueError(f"invalid regex pattern: {exc}") from exc

    def message_of(line: str) -> str:
        return line.split(" ", 1)[1] if " " in line else line

    kept: list[str] = []
    for line in lines:
        msg = message_of(line)
        if grep_patterns and not any(p.search(msg) for p in grep_patterns):
            continue
        if any(p.search(msg) for p in exclude_patterns):
            continue
        kept.append(line)
    return kept


def cmd_git_log(args: argparse.Namespace) -> None:
    lines = _open_store(args).git_log(limit=args.limit)
    _emit(_filter_log_lines(lines, args.grep or [], args.exclude or []))


def cmd_health_snapshot(args: argparse.Namespace) -> None:
    # --now 钉住 store clock（decay 同型）：行内 date 与 stats 窗口同一基准。
    if args.now:
        store = MemoryStore(Path(args.root), clock=lambda: dt.date.fromisoformat(args.now))
    else:
        store = _open_store(args)
    # date 走注入 clock 的公开出口 today()（extraction 同型），CLI 不另算今天。
    _emit(append_snapshot(Path(args.root), store.stats(), store.today()))


def _decay_enabled(args: argparse.Namespace) -> bool:
    """衰减默认关。--with-decay、RUN_DECAY=1、DECAY=1 任一即开，整次只跑一遍。"""
    if args.with_decay:
        return True
    return os.environ.get("RUN_DECAY") == "1" or os.environ.get("DECAY") == "1"


def _maintain_store(args: argparse.Namespace) -> MemoryStore:
    """与 dream/decay 同开库；--now 钉住 clock，供衰减谓词与快照日期共用。"""
    if args.now:
        day = dt.date.fromisoformat(args.now)
        return MemoryStore(
            Path(args.root),
            embedder=auto_encoder(),
            agent_id=_agent_id_from_env(),
            clock=lambda: day,
        )
    return _open_store(args)


def _maintain_log(message: str) -> None:
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    print(f"[{stamp}] maintain: {message}", file=sys.stderr)


def _fail_step(step: str, exc: Exception) -> int:
    """点名失败步骤后非 0 退出。调用方错误仍走 main 的 exit 2 翻译。"""
    _maintain_log(f"FAILED step={step}: {exc}")
    if isinstance(exc, (ValueError, PermissionError, WriteLockError)):
        raise exc
    return 1


def cmd_maintain(args: argparse.Namespace) -> int:
    """dream（只读报告落盘）→ 可选 decay → health-snapshot。任一步失败即停。"""
    store = _maintain_store(args)
    ran_decay = _decay_enabled(args)
    plan_path = last_plan_path(store.root)
    steps: list[str] = []

    _maintain_log("step=dream")
    try:
        report = store.dream(
            window_days=args.window,
            min_uses=args.min_uses,
            min_confidence=args.min_confidence,
            ns=args.ns,
            reader=args.reader,
        )
        # 摘要走 stderr，与 dream 命令同型；文件里是完整只读报告，不含 --propose 产物。
        print(report["summary"], file=sys.stderr)
        atomic_write_text(
            plan_path,
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        )
    except Exception as exc:
        return _fail_step("dream", exc)
    steps.append("dream")
    _maintain_log(f"step=dream ok plan={plan_path}")

    archived: list[str] = []
    if ran_decay:
        _maintain_log("step=decay")
        try:
            archived = store.decay_sweep()
        except Exception as exc:
            return _fail_step("decay", exc)
        steps.append("decay")
        _maintain_log(f"step=decay ok archived={len(archived)}")
    else:
        _maintain_log("step=decay skipped")

    _maintain_log("step=health-snapshot")
    try:
        # 快照在 decay 之后：衰减发生时，行内 stats 反映归档后的库。
        snap = append_snapshot(store.root, store.stats(), store.today())
    except Exception as exc:
        return _fail_step("health-snapshot", exc)
    steps.append("health-snapshot")
    _maintain_log(f"step=health-snapshot ok path={snap['path']}")
    _maintain_log("done")

    payload: dict[str, Any] = {
        "ok": True,
        "steps": steps,
        "decay": ran_decay,
        "plan": str(plan_path),
        "health": {"ok": True, "date": snap["date"], "path": snap["path"]},
    }
    if ran_decay:
        payload["archived"] = archived
    _emit(payload)
    return 0


def cmd_health_trend(args: argparse.Namespace) -> int:
    # 读面纯文件读：不开 store（无 git/锁/索引副作用）；空态是显式结果而非失败。
    # 锚定日因此与 snapshot 的 store.today() 是两源——不开 store 是读面的设计决策，
    # 跨午夜调用时窗口锚可能差一天，属可接受边界（--now 可显式钉住）。
    today = dt.date.fromisoformat(args.now) if args.now else dt.date.today()
    report = trend(Path(args.root), args.days, today)
    if report["status"] == "empty":
        print(f"health-trend: empty ({report['reason']})", file=sys.stderr)
    _emit(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compound-memory", description="local multi-agent shared memory")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", default=None, help="store root (default: $COMPOUND_MEMORY_ROOT or ~/.agents/memory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)

    p = sub.add_parser("write")
    p.add_argument("content"); p.add_argument("type", choices=MEMORY_TYPES)
    p.add_argument("source"); p.add_argument("--ns", default="_shared"); p.add_argument("--key", default=None)
    p.add_argument("--valid-from", default=None, help="ISO date: fact valid from (annotation)")
    p.add_argument("--valid-until", default=None,
                   help="ISO date: fact expires after this day (excluded from search, still readable via get)")
    p.add_argument("--project", default=None,
                   help="project scope slug for workspace-specific memories "
                        "(omitted = global; falls back to $COMPOUND_MEMORY_PROJECT)")
    p.add_argument("--lesson-role", default=None, choices=["lesson", "avoid"],
                   help="optional lesson semantics: lesson (usually insight) or avoid "
                        "(decision + this field). Omit for an ordinary memory. "
                        "Do not encode the role in the key.")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--ns", default=None,
                   help="scope: '_shared', 'agent-<name>', or omit for _shared + your own private ns")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.add_argument("--no-neighbors", dest="include_neighbors", action="store_false",
                   help="omit embedded one-hop neighbors from hits")
    p.add_argument("--project", default=None,
                   help="project scope: see global memories plus this project's "
                        "(omitted = global only, fail-closed; falls back to $COMPOUND_MEMORY_PROJECT)")
    p.add_argument("--explain", action="store_true",
                   help="attach per-hit ranking components (ranks, RRF score, prior terms, channel) "
                        "and an evidence summary line to each hit (debugging carrier)")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("get")
    p.add_argument("id")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.add_argument("--project", default=None,
                   help="project scope for neighbor filtering (get itself is always readable; "
                        "falls back to $COMPOUND_MEMORY_PROJECT)")
    p.set_defaults(func=cmd_get)
    p = sub.add_parser(
        "explain",
        help="confidence/evidence composition of one memory (by-id evidence view)",
        description="By-id evidence view (ADR-0008 presentation boundary): the memory's evidence block "
        "counts (success/failure/contradiction, last_verified, recent outcome details), cross-host "
        "validation list (validated_by), derivation marker (origin=distillation products start from an "
        "explicit zero evidence block and never fold evidence back into their sources), and the current "
        "confidence. Read-only: no commit. Private agent-* namespaces are owner-only (--reader, same "
        "rule as get). Unknown ids return {\"found\": false}.",
    )
    p.add_argument("id")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_explain)
    p = sub.add_parser("link")
    p.add_argument("a"); p.add_argument("b")
    p.add_argument("--agent", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_link)
    p = sub.add_parser("feedback"); p.add_argument("id"); p.add_argument("agent")
    p.add_argument("--outcome", default="success",
                   help="feedback outcome: success (default) | failure | contradiction | obsolete | unknown")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("decay"); p.add_argument("--now", default=None, help="ISO date override (testing)")
    p.set_defaults(func=cmd_decay)

    p = sub.add_parser("revive")
    p.add_argument("id")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_revive)
    p = sub.add_parser(
        "forget",
        help="terminally remove a memory (ADR-0009): file physically removed, one audit commit; content survives only in git history",
        description="Terminal forget (ADR-0009): the memory file is physically removed "
        "(active or archive area) and exactly one forget commit keeps the audit trail — content "
        "survives only in git history, recovery is out-of-band git surgery (checkout/revert), there "
        "is no in-system revive. Idempotent: an unknown or already-forgotten id returns "
        '{"found": false}. Private agent-* namespaces are owner-only (--agent, same rule as '
        "feedback). forget solves 'the active library no longer carries it', not 'history no longer "
        "contains it' — history cleanup stays a destructive out-of-band operation. The optional "
        "--reason is a motive phrase, never memory content (single line, capped at 80 chars).",
    )
    p.add_argument("id")
    p.add_argument("--agent", required=True, help="caller identity (agent role; private ns is owner-only)")
    p.add_argument("--reason", default=None, help="optional motive phrase (single line, max 80 chars)")
    p.set_defaults(func=cmd_forget)
    p = sub.add_parser(
        "distill-plan",
        help="scan distillation candidates and print a signal-annotated list",
        description="Scan distillation candidates (deterministic half of distillation). Signals per candidate: "
        f"merge_with (same ns/type/key, strong), possible_dup_of (BM25 normalized_similarity >= {DISTILL_DUP_SIM_THRESHOLD}, weak), "
        f"promotion_candidate (episode uses >= {PROMOTION_USES_THRESHOLD}). "
        f"suggested_action is one of {', '.join(SUGGESTED_ACTIONS)}, or null when none of those signals fire. "
        "suggested_actions lists clusters: same-key groups are merge and carry source_ids "
        "(auto-listed, including below the activity gate); promotion_candidate is promote; "
        "possible_dup_of, including cross-key weak similarity, is review. "
        "Cross-key multi-source consolidation uses the same merge label and a source_ids cluster — "
        "there is no synthesize action and no supersedes frontmatter. "
        "Only a promote row carries target_type skill. If an active skill in the same ns already "
        "uses that episode's key, the row is review and omits target_type. merge and review rows "
        "omit target_type. This command does not write skill step text. "
        "negative_salience and contradiction_pairs are report-only (failure_count, low confidence, "
        "contradiction signals, lesson_role avoid, same-key content conflicts, and contradicts: links). "
        "They do not add a suggested_action and do not widen dream-apply. "
        "Judgment (merge/summarize) stays with the calling agent.",
    )
    p.add_argument("--window", type=int, default=30, help="recency window in days (last_used first, created fallback)")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate: uses >= this")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate: confidence >= this")
    p.add_argument("--ns", default="_shared")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_distill_plan)
    p = sub.add_parser(
        "dream",
        help="read-only offline consolidation report (optional --propose writes a proposals file)",
        description="Offline consolidation prep for one namespace (ADR-0001: deterministic prep, "
        "judgment stays with the calling agent; no in-process LLM). Read-only dry-run. "
        "The distill section is the distill-plan result (merge_with / possible_dup_of / "
        "promotion_candidate / key_duplicates / suggested_action / negative_salience / "
        "contradiction_pairs). suggested_action is merge, promote, or review. "
        "negative_salience and contradiction_pairs are report-only and do not widen dream-apply. "
        "decay_signals counts valid_until already passed "
        "(expired_count), the archive area (archived_count), and low-activity memories that match "
        "the decay archive predicate (low_activity_count) — counts only, nothing is archived. "
        "stats_before and stats_after are two stats() snapshots and are equal on this path. "
        "Does not archive, distill-apply, forget, or write feedback. Not an MCP tool. "
        "The explicit gated write command is dream-apply; this command has no apply switch. "
        "Optional --propose (ADR-0011, default off) writes "
        "<root>/distill/dream-proposals.json: a JSON list shaped for dream-apply "
        "(content, type, source_ids, key). The system proposes; a human runs dream-apply. "
        "No qualifying cluster writes an empty list. Stdout stays the read-only report. "
        "JSON on stdout; a one-line summary on stderr. With --propose, stderr adds a line "
        "naming the proposals file.",
    )
    p.add_argument("--window", type=int, default=30, help="recency window in days, passed through to distill-plan")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate passed through to distill-plan")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate passed through to distill-plan")
    p.add_argument("--ns", default="_shared")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.add_argument(
        "--propose",
        action="store_true",
        help="write <root>/distill/dream-proposals.json (dream-apply JSON list; "
        "empty list when nothing qualifies). Does not apply. Default off.",
    )
    p.set_defaults(func=cmd_dream)
    p = sub.add_parser(
        "dream-apply",
        help="explicitly apply gated dream signals via distill-apply (caller supplies the product)",
        description="Explicit half-automatic apply for one namespace (ADR-0001 / #72). Separate from "
        "read-only dream: timers and distill-prepare must not call this. No in-process LLM. Not an MCP tool. "
        "Proposals are a JSON list. Each object must include content, type, and source_ids (optional key, "
        "confidence, and promote). Nothing is written until that body is present — there is no silent "
        "keep-new/archive-old. Admitted signals: merge_with and key_duplicates, plus promotion_candidate "
        "only when that object sets promote true. A promotion_candidate whose type is skill is "
        "skipped, and distill-apply is not called, when an active skill in that ns already uses "
        "the submitted key. possible_dup_of is report-only and never admitted. "
        "Empty content rejects the whole list. This command does not invent skill step text. "
        "Expired and low-activity rows are not admitted. Admitted rows are executed one by one through "
        "distill-apply (origin stays distillation). Skipped rows are listed with a reason. "
        "--dry-run prints the plan and writes nothing. Does not forget, and there is no --include-forget "
        "and no git-revert helper (use git-log). Private agent-* namespaces need --reader, and --source "
        "must be the owner. JSON on stdout; a one-line count summary on stderr.",
    )
    p.add_argument("--proposals", required=True, help="JSON list of products (content, type, source_ids)")
    p.add_argument("--source", required=True, help="writer identity passed through to distill-apply")
    p.add_argument("--dry-run", action="store_true", help="print planned actions and write nothing")
    p.add_argument("--window", type=int, default=30, help="recency window in days, passed through to distill-plan")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate passed through to distill-plan")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate passed through to distill-plan")
    p.add_argument("--ns", default="_shared")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_dream_apply)
    p = sub.add_parser("distill-apply")
    p.add_argument("content"); p.add_argument("type", choices=MEMORY_TYPES); p.add_argument("source")
    p.add_argument("--sources", required=True, help="comma-separated source memory ids")
    p.add_argument("--ns", default="_shared"); p.add_argument("--key", default=None)
    p.add_argument("--confidence", type=float, default=None)
    p.set_defaults(func=cmd_distill_apply)
    p = sub.add_parser(
        "stats",
        help="health counters; exit 1 when COMPOUND_MEMORY_AGENT_ID is unset or blank",
        description="Health counters (histograms, liveness, distillation yield) plus retrieval and "
        "attestation. JSON on stdout; attestation and lexical-only notes on stderr. "
        "Exit 0 when COMPOUND_MEMORY_AGENT_ID is set to a non-blank id. "
        "Exit 1 when it is unset or blank — sharing _shared without a process identity is unsafe. "
        "The JSON is still printed. Lexical retrieval is a note, not a failure. "
        "Caller errors stay exit 2. MCP tools are not failed by a missing agent id; use doctor "
        "for root, write-lock, and git checks.",
    )
    p.set_defaults(func=cmd_stats)
    p = sub.add_parser(
        "doctor",
        help="one-page health diagnosis; exit 1 on hard failures (unset AGENT_ID, missing root, broken lock, unusable git)",
        description="One-page health diagnosis for install and multi-host checks. "
        "JSON on stdout; warnings on stderr. "
        "Exit 0 when no hard problem is present. "
        "Exit 1 when COMPOUND_MEMORY_AGENT_ID is unset or blank, the store root is missing "
        "or not a directory, the write lock cannot be acquired, or the memory root is not a "
        "usable git repository (git missing, no .git, or git status failed). "
        "A lexical retrieval mode and a dirty worktree are reported and do not by themselves "
        "fail the check. "
        "Does not create a missing root and does not git-init a directory that is not already "
        "a repository. Git status is read before store startup, so a dirty tree is visible "
        "before orphan recovery. "
        "Caller errors stay exit 2. MCP tool calls keep running when the agent id is unset; "
        "this command is the operator health check, not a mid-session gate. "
        "stats uses the same attestation rule and also exits 1 when the agent id is unset.",
    )
    p.set_defaults(func=cmd_doctor)
    p = sub.add_parser("rebuild-index"); p.set_defaults(func=cmd_rebuild_index)
    p = sub.add_parser("review-queue"); p.set_defaults(func=cmd_review_queue)
    p = sub.add_parser(
        "review-resolve",
        help="mark review-queue conflicts as resolved and archive the dropped side",
        description="Clear review-queue entries after the caller has judged the conflict "
        "(old/new trade-off stays with the calling agent or human). Pass the dropped memory "
        "ids: matching rows are cleared and the passed id (the discarded side) is archived "
        "automatically, the surviving side stays active. --all clears the queue without "
        "archiving (rows carry no verdict). For contradiction rows, --uphold flips the "
        "verdict: the disputed memory is kept, its confidence unfreezes and the dispute "
        "folds as a failure (-0.2). Without --uphold, contradiction rows resolve as "
        "confirm-wrong (the passed id is archived). Unknown ids are rejected atomically; "
        "the cleanup is auto-committed.",
    )
    p.add_argument("ids", nargs="*", metavar="ID")
    p.add_argument("--all", action="store_true", help="clear the whole queue")
    p.add_argument("--uphold", action="store_true",
                   help="uphold the disputed memories instead of archiving (contradiction rows only): "
                        "unfreeze confidence and fold the dispute as failure -0.2")
    p.add_argument("--reader", default=None,
                   help="caller identity, required to resolve rows from private agent-* namespaces")
    p.set_defaults(func=cmd_review_resolve)
    p = sub.add_parser("git-log")
    p.add_argument("--limit", type=int, default=5, help="number of commits to fetch before filtering")
    p.add_argument(
        "--grep", action="append", default=None, metavar="PATTERN",
        help="keep only commits whose message matches PATTERN (regex, repeatable, any-match OR)",
    )
    p.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="drop commits whose message matches PATTERN (regex, repeatable; applied after --grep)",
    )
    p.set_defaults(func=cmd_git_log)
    p = sub.add_parser(
        "extract",
        help="scan a session transcript for memory candidates (deterministic pass, no LLM)",
        description="Deterministic candidate extraction from a session transcript. Transcript shapes are "
        "auto-detected by content (not filename): "
        + supported_hosts_summary_en()
        + ". Pass a directory to batch-scan every supported session file under it "
        "(<project>/<session>.jsonl layouts and <project>/<session>/session.jsonl.zstd; subagents/ skipped — "
        "their role:user is the team-lead agent's task brief, not the human's own statement). "
        "ZCode rollout/model-io snapshots and WorkBuddy traces/ are deliberately unsupported — they keep only "
        "the most recent / first turns, so accepting them would look like a scan while silently dropping most "
        "of the history. "
        "Pattern matching only (statement -> fact, pitfall -> insight); the manifest lands in "
        "<root>/extract/last-candidates.json. Writing stays with the agent: confirm each candidate "
        "via memory_write (same-key conflicts still enter the review queue).",
    )
    p.add_argument(
        "transcript",
        help="path to a session log jsonl (WorkBuddy / Claude Code), the ZCode session database (.sqlite), "
        "a DeepSeek Harness session file (.jsonl.zstd), or a directory of session logs",
    )
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser(
        "health-snapshot",
        help="append today's stats snapshot to health/health.jsonl (ops/timer verb)",
        description="Append one timestamped line to <root>/health/health.jsonl: the line is the "
        "ISO date plus the stats() payload verbatim — stats stays the single source of metrics, "
        "nothing is recomputed here. Explicit ops action for a human or a launchd/cron timer. "
        "Single-writer assumption: the append does not take the write lock — run one timer or "
        "one manual call at a time. Multiple calls on the same day append multiple lines; daily "
        "dedup belongs to health-trend (last row per day wins). health/ is a runtime artifact "
        "directory (gitignored like index/ and extract/): health history is not part of the git "
        "audit trail. Not an MCP tool. --now pins both the store clock and the appended date "
        "(testing).",
    )
    p.add_argument("--now", default=None, help="ISO date override (testing)")
    p.set_defaults(func=cmd_health_snapshot)
    p = sub.add_parser(
        "health-trend",
        help="summarize the last N days of health snapshots (per-day last row + first/last delta)",
        description="Read <root>/health/health.jsonl and summarize the window: one row per day "
        "(the LAST line of that day — same-day repeats are deduped here, not on write) with "
        "total, archive rate (archived/total), avg confidence, review-queue length, "
        "distillation yield (distilled_recent_7d), cross-host coverage (cross_validated/total), "
        "and recent_feedback_7d, plus a first-vs-last delta over the window. Rates and deltas "
        "are computed at read time — no derived values are stored. Malformed lines are skipped "
        "and counted (skipped_lines). A missing file or no data in the window prints an "
        "explicit empty state on stdout and stderr and exits 0. Read-only: opens no store, "
        "takes no lock, writes nothing. Not an MCP tool. --days defaults to 7. --now pins the "
        "window anchor date (testing).",
    )
    p.add_argument("--days", type=int, default=7, help="window in days ending today (inclusive)")
    p.add_argument("--now", default=None, help="ISO date override (testing)")
    p.set_defaults(func=cmd_health_trend)
    p = sub.add_parser(
        "maintain",
        help="daily ops: read-only dream, optional decay, then health-snapshot",
        description="One operator pass for a launchd/cron job. Order is fixed: "
        "read-only dream (writes <root>/distill/last-plan.json; same report as "
        "`dream`, never --propose) → decay only when --with-decay or RUN_DECAY=1 "
        "(DECAY=1 is an alias) → health-snapshot, so the snapshot sees post-decay "
        "stats when decay ran. Default is dream + health-snapshot; decay stays off. "
        "Does not call dream-apply or distill-apply. A failing step exits non-zero "
        "and names that step; later steps do not run. "
        "scripts/distill-prepare.sh remains the dream-only prep path. "
        "scripts/daily-maintain.sh is a thin uv/PATH wrapper around this command. "
        "Not an MCP tool. --propose and apply stay out of timers (ADR-0001 / #72 / #97).",
    )
    p.add_argument("--with-decay", action="store_true", help="run decay before health-snapshot (default off)")
    p.add_argument("--now", default=None, help="ISO date override for the store clock (testing)")
    p.add_argument("--window", type=int, default=30, help="recency window in days, passed through to dream")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate passed through to dream")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate passed through to dream")
    p.add_argument("--ns", default="_shared")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_maintain)
    return parser


# type=float 的选项。argparse 把下一个以 `-` 开头的 token 当成新旗标，
# `--confidence -inf` / `--confidence -0.1` 会在校验前变成
# "expected one argument"（#110）。能 float() 的值先拼成 `--opt=value`。
_FLOAT_OPTIONS = frozenset({"--confidence", "--min-confidence"})


def _is_float_token(token: str) -> bool:
    """下一个 argv 是否是 float() 能读的数（含 nan / inf / 负数）。

    长选项（`--ns`）不是数值，留给原来的旗标解析。
    """
    if token.startswith("--"):
        return False
    try:
        float(token)
    except ValueError:
        return False
    return True


def _bind_float_options(argv: list[str]) -> list[str]:
    """把 `--confidence -inf` 收成 `--confidence=-inf`，再交给 argparse。

    已经是 `--confidence=nan` 的形式不动。拼不上的下一个 token（不是数）
    留给 argparse 报 invalid float。校验仍在 write_new，这里只保证数值进得了参数。
    """
    bound: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if (
            token in _FLOAT_OPTIONS
            and index + 1 < len(argv)
            and _is_float_token(argv[index + 1])
        ):
            bound.append(f"{token}={argv[index + 1]}")
            index += 2
            continue
        bound.append(token)
        index += 1
    return bound


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = build_parser().parse_args(_bind_float_options(list(argv)))
    if args.root is None:
        args.root = default_root()
    try:
        code = args.func(args)
    except (ValueError, PermissionError, WriteLockError) as exc:
        # store 接口的调用方错误统一在这里翻译成 JSON（MCP 侧由框架转 is_error）。
        # WriteLockError 是争锁失败的 fail-closed（#58），同样不该变成 traceback。
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    # stats / doctor 在硬失败时返回 1；其余命令返回 None，保持退出码 0。
    return 0 if code is None else code


if __name__ == "__main__":
    sys.exit(main())
