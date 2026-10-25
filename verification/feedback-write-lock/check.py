#!/usr/bin/env python3
"""Re-check NoLostFeedbackUpdate against the working tree.

One command, from the repo root:

    uv run python verification/feedback-write-lock/check.py

Exit 0 only when all of the following hold:

1. The skill and the TLA header name the same sync commit.
2. Correspondence: feedback's find, _record_event, and _save sit in the one
   ``with store._write_lock()`` body, and write_lock still acquires the mutex
   before it reads ``self._local.lock_depth``, and every yield is before
   ``_mutex.release``.
3. Runtime: a real MemoryStore.feedback calls find and _save while
   ``_write_lock_depth`` is at least 1.
4. TLC on FeedbackWriteLock.cfg (ReadOutsideLock = FALSE) finds no error,
   and the state graph is deep enough that both feedbacks can finish.
5. TLC on FeedbackWriteLockBroken.cfg (ReadOutsideLock = TRUE) violates
   NoLostFeedbackUpdate. A pass there means this check can no longer fail.

The checker imports ``src/`` on purpose, so a scratch edit of lifecycle.py
is what runs. It does not ask TLC to parse Python.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC_DIR = Path(__file__).resolve().parent
SRC = REPO / "src"
LIFECYCLE = SRC / "compound_memory" / "storage" / "lifecycle.py"
LOCKING = SRC / "compound_memory" / "storage" / "locking.py"
SKILL = REPO / ".cursor" / "skills" / "verify-feedback-write-lock" / "SKILL.md"
SPEC = SPEC_DIR / "FeedbackWriteLock.tla"

TLC_VERSION = "1.7.4"  # TLC2 Version 2.19 of 08 August 2024
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"

COVERED = (
    "src/compound_memory/storage/lifecycle.py",
    "src/compound_memory/storage/locking.py",
    "src/compound_memory/storage/files.py",
    "src/compound_memory/storage/facade.py",
)

SHA_RE = re.compile(r"sync_commit:\s*([0-9a-f]{40})")
INSIDE_CALLS = ("find", "_record_event", "_save", "fold_success", "fold_failure")


class CheckError(Exception):
    """The check failed. The message is the whole reason, in the code's words."""


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise CheckError(f"CORRESPONDENCE FAIL: {name} is gone from the file the model names")


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_write_lock_with(node: ast.With) -> bool:
    if len(node.items) != 1:
        return False
    ctx = node.items[0].context_expr
    if not isinstance(ctx, ast.Call) or not isinstance(ctx.func, ast.Attribute):
        return False
    return ctx.func.attr == "_write_lock"


def check_feedback_correspondence() -> None:
    """find, the uses increment, and _save are one with-body. That is the critical section."""
    fn = _function(_parse(LIFECYCLE), "feedback")
    locks = [n for n in ast.walk(fn) if isinstance(n, ast.With) and _is_write_lock_with(n)]
    if len(locks) != 1:
        raise CheckError(
            "CORRESPONDENCE FAIL: lifecycle.feedback no longer has exactly one "
            "`with store._write_lock()` (locking.py WriteLocker.write_lock). "
            f"Found {len(locks)}. The model treats that with as one critical section "
            "around find, _record_event, and _save."
        )
    body = locks[0]
    inside_ids = {id(n) for stmt in body.body for n in ast.walk(stmt)}
    outside: list[str] = []
    for node in ast.walk(fn):
        if id(node) in inside_ids:
            continue
        called = _call_name(node)
        if called in ("find", "_record_event", "_save"):
            outside.append(called)
    # A call that moved out of the with is the lost update, not a rename.
    if outside:
        raise CheckError(
            "PROPERTY FAIL: lifecycle.feedback calls "
            + ", ".join(sorted(set(outside)))
            + " outside `with store._write_lock()`. "
            "find outside the lock is the lost-update bug: two feedbacks read the "
            "same uses snapshot and the later _save overwrites the earlier increment "
            "(lifecycle.py feedback, locking.py write_lock). "
            "The TLA constant ReadOutsideLock = TRUE is this placement. "
            "Do not move the call back into the model by setting the constant TRUE "
            "on the passing config."
        )
    inside = _calls_named_ids(body.body)
    missing = [name for name in INSIDE_CALLS if name not in inside]
    if missing:
        raise CheckError(
            "CORRESPONDENCE FAIL: lifecycle.feedback's `with store._write_lock()` body "
            f"no longer calls {missing}. NoLostFeedbackUpdate needs find, "
            "_record_event (mem.uses += 1), fold_success/fold_failure, and _save "
            "in that same body. _save writes uses and confidence together."
        )


def _calls_named_ids(stmts: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for stmt in stmts:
        for node in ast.walk(stmt):
            called = _call_name(node)
            if called is not None:
                names.add(called)
    return names


def _is_local_lock_depth(node: ast.AST) -> bool:
    """self._local.lock_depth — thread-local reentry, not another thread's depth."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "lock_depth"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "_local"
    )


def _depth_tests(fn: ast.AST) -> list[ast.If]:
    found: list[ast.If] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and any(_attr_named(node.test, "lock_depth")):
            found.append(node)
    return found


def _attr_named(tree: ast.AST, attr: str) -> list[ast.Attribute]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == attr
    ]


def _call_lines(fn: ast.AST, attr: str, receiver: str | None = None) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != attr:
            continue
        if receiver is not None:
            recv = node.func.value
            if not (isinstance(recv, ast.Attribute) and recv.attr == receiver):
                continue
        lines.append(node.lineno)
    return lines


def check_write_lock_correspondence() -> None:
    """Mutex first, then this thread's depth, yields before release. Matches the model."""
    fn = _function(_parse(LOCKING), "write_lock")
    acquire = _call_lines(fn, "acquire", "_mutex")
    release = _call_lines(fn, "release", "_mutex")
    flocks = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "_lock_exclusive")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "_lock_exclusive")
        )
    ]
    yields = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Yield)]
    tests = _depth_tests(fn)
    if not acquire or not release or not flocks or len(yields) < 1 or len(tests) != 1:
        raise CheckError(
            "CORRESPONDENCE FAIL: locking.WriteLocker.write_lock no longer has the "
            "steps the model names (self._mutex.acquire, one self._local.lock_depth "
            f"test, _lock_exclusive, yield, self._mutex.release). "
            f"Found acquire={acquire}, release={release}, flock={flocks}, "
            f"yields={yields}, depth_tests={len(tests)}."
        )
    test = tests[0]
    if not any(_is_local_lock_depth(n) for n in ast.walk(test.test)):
        raise CheckError(
            "PROPERTY FAIL: write_lock's reentry test no longer reads "
            "self._local.lock_depth (locking.py). A shared depth lets the other "
            "thread take the reentry path, skip _lock_exclusive, and enter the "
            "critical section (#107). NoLostFeedbackUpdate fails on that step order."
        )
    if min(acquire) >= test.lineno:
        raise CheckError(
            "PROPERTY FAIL: write_lock reads lock_depth before self._mutex.acquire "
            "(locking.py). The other thread can pass the reentry test while this "
            "thread is inside the critical section and skip the flock. "
            "The mutex has to be held before the depth test, and across yield, "
            "or the two feedbacks overlap."
        )
    if max(yields) > min(release):
        raise CheckError(
            "PROPERTY FAIL: write_lock yields after self._mutex.release (locking.py). "
            "Releasing the mutex before the critical section ends is the deadlock "
            "noted above write_lock, and it lets the waiter run while this thread "
            "is still inside find / _save."
        )
    if min(flocks) < test.lineno:
        raise CheckError(
            "CORRESPONDENCE FAIL: _lock_exclusive now happens before the "
            "self._local.lock_depth test. The model takes the flock only on the "
            "depth == 0 arm (locking.py write_lock)."
        )


def check_sync_commit() -> str:
    skill = SKILL.read_text(encoding="utf-8")
    spec = SPEC.read_text(encoding="utf-8")
    skill_shas = SHA_RE.findall(skill)
    spec_shas = SHA_RE.findall(spec)
    if skill_shas != spec_shas or len(skill_shas) != 1:
        raise CheckError(
            "CORRESPONDENCE FAIL: sync_commit in the skill and in "
            "FeedbackWriteLock.tla must be the same single SHA. "
            f"skill={skill_shas} spec={spec_shas}. "
            "Move it only after this command passes on the code at that commit."
        )
    sha = skill_shas[0]
    diff = subprocess.run(
        ["git", "diff", "--name-only", sha, "--", *COVERED],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    work = subprocess.run(
        ["git", "diff", "--name-only", "--", *COVERED],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
    )
    changed = sorted({*diff.stdout.split(), *work.stdout.split()})
    if diff.returncode != 0:
        print(f"note: git diff against sync_commit {sha} failed; still checking the working tree")
    elif changed:
        print(
            "note: covered files differ from sync_commit "
            f"{sha}: {', '.join(changed)}. "
            "The check below runs on the working tree. "
            "If it passes and the TLA actions still match the new steps, "
            "advance sync_commit in the skill and the spec together. "
            "If it fails, treat the failure as the property, not as a stale SHA."
        )
    else:
        print(f"sync_commit {sha} still matches the covered files")
    return sha


def check_runtime() -> None:
    """Real feedback, real write_lock. find and _save must observe the lock held."""
    sys.path.insert(0, str(SRC))
    from compound_memory.storage import MemoryStore

    root = Path(tempfile.mkdtemp(prefix="fv-feedback-lock-"))
    try:
        store = MemoryStore(root, git=False, clock=lambda: dt.date(2026, 10, 5))
        seen: dict[str, list[int]] = {"find": [], "save": []}
        real_find = store.find
        real_save = store._save
        armed = {"on": False}

        def spy_find(mem_id: str):
            if armed["on"]:
                seen["find"].append(store._write_lock_depth)
            return real_find(mem_id)

        def spy_save(mem: object) -> None:
            if armed["on"]:
                seen["save"].append(store._write_lock_depth)
            real_save(mem)  # type: ignore[arg-type]

        store.find = spy_find  # type: ignore[method-assign]
        store._save = spy_save  # type: ignore[method-assign]
        written = store.write(
            "feedback lock probe",
            type="fact",
            source="agent-a",
            key="feedback-lock-probe",
        )
        armed["on"] = True
        store.feedback(written["id"], "agent-b")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if not seen["find"] or not seen["save"]:
        raise CheckError(
            "PROPERTY FAIL: MemoryStore.feedback did not call both find and _save "
            f"(find depths={seen['find']}, save depths={seen['save']}). "
            "The success path in lifecycle.feedback must read and write the memory."
        )
    if any(depth < 1 for depth in seen["find"]):
        raise CheckError(
            "PROPERTY FAIL: lifecycle.feedback called find while _write_lock_depth "
            f"was {seen['find']} (facade._write_lock_depth / locking.WriteLocker.lock_depth). "
            "Depth 0 means find ran outside write_lock. Two feedbacks can then read "
            "the same uses and the later _save drops one increment."
        )
    if any(depth < 1 for depth in seen["save"]):
        raise CheckError(
            "PROPERTY FAIL: lifecycle.feedback called _save while _write_lock_depth "
            f"was {seen['save']}. _save outside the lock writes a snapshot that "
            "another feedback may already have superseded."
        )
    print(
        "runtime: MemoryStore.feedback find and _save ran at "
        f"_write_lock_depth find={seen['find']} save={seen['save']}"
    )


def _jar() -> Path:
    cache = SPEC_DIR / ".cache"
    cache.mkdir(exist_ok=True)
    jar = cache / f"tla2tools-{TLC_VERSION}.jar"
    if jar.is_file() and hashlib.sha256(jar.read_bytes()).hexdigest() == TLC_SHA256:
        return jar
    print(f"downloading TLC {TLC_VERSION} from {TLC_URL}")
    try:
        urllib.request.urlretrieve(TLC_URL, jar)  # noqa: S310 — pinned release URL
    except OSError as exc:
        raise CheckError(f"TOOL FAIL: could not download TLC {TLC_VERSION}: {exc}") from exc
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    if digest != TLC_SHA256:
        jar.unlink(missing_ok=True)
        raise CheckError(
            f"TOOL FAIL: TLC jar sha256 {digest} != pinned {TLC_SHA256}. "
            "The download is not the TLC this check was run with."
        )
    return jar


def _run_tlc(config: str) -> tuple[int, str]:
    java = shutil.which("java")
    if java is None:
        raise CheckError(
            "TOOL FAIL: java is not on PATH. TLC 2.19 (tlaplus 1.7.4) needs a Java 11+ runtime."
        )
    jar = _jar()
    meta = Path(tempfile.mkdtemp(prefix="tlc-feedback-lock-"))
    cmd = [
        java,
        "-XX:+UseParallelGC",
        "-Xmx512m",
        "-jar",
        str(jar),
        "-workers",
        "1",
        "-metadir",
        str(meta),
        "-config",
        config,
        "FeedbackWriteLock.tla",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=SPEC_DIR,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(meta, ignore_errors=True)
    return proc.returncode, proc.stdout + proc.stderr


def _summary(output: str) -> str:
    keep: list[str] = []
    for line in output.splitlines():
        if (
            "Model checking completed" in line
            or "Invariant " in line
            or "distinct states" in line
            or "depth of the complete" in line
            or "Error:" in line
            or line.startswith("State ")
            or line.startswith("/\\ ")
            or line.startswith("==")
        ):
            keep.append(line)
    return "\n".join(keep) if keep else output[-2000:]


def check_tlc() -> None:
    code, out = _run_tlc("FeedbackWriteLock.cfg")
    distinct = re.search(r"(\d+) distinct states found", out)
    depth = re.search(r"depth of the complete state graph search is (\d+)", out)
    if code != 0 or "No error has been found" not in out:
        raise CheckError(
            "MODEL FAIL: TLC on FeedbackWriteLock.cfg (ReadOutsideLock = FALSE) "
            "did not pass. Correspondence said the working tree still matches the "
            "model's step order, so either the model is wrong or TLC hit a tool "
            "error. Do not change product code to silence this. Read the trace "
            "with /statechart. If you can replay it through MemoryStore.feedback "
            "and watch uses drop, it is a real bug; report it and leave the property.\n"
            + out
        )
    n_states = int(distinct.group(1)) if distinct else 0
    n_depth = int(depth.group(1)) if depth else 0
    if n_states < 40 or n_depth < 20:
        raise CheckError(
            "MODEL FAIL: the passing run is too small to have scheduled both "
            f"feedbacks (distinct states={n_states}, depth={n_depth}). "
            "A vacuous model is not a proof. NoLostFeedbackUpdate was not shown "
            "to survive two finished feedbacks.\n"
            + _summary(out)
        )
    print(f"tlc correct: no error, {n_states} distinct states, depth {n_depth}")

    bad_code, bad_out = _run_tlc("FeedbackWriteLockBroken.cfg")
    if bad_code == 0 or "Invariant NoLostFeedbackUpdate is violated" not in bad_out:
        raise CheckError(
            "BROKEN-MODEL FAIL: FeedbackWriteLockBroken.cfg (find before the lock, "
            "ReadOutsideLock = TRUE) did not violate NoLostFeedbackUpdate. "
            "The property can no longer fail, so a pass of the correct config "
            "proves nothing. Restore a broken variant that loses the uses update.\n"
            + bad_out
        )
    if "Invariant TypeOK is violated" in bad_out:
        raise CheckError(
            "MODEL FAIL: the broken run violated TypeOK before it could show "
            "the lost update. That is a modeling error.\n" + bad_out
        )
    if 'uses = 1' not in bad_out or '|-> "done"' not in bad_out:
        raise CheckError(
            "BROKEN-MODEL FAIL: TLC reported a violation but the trace does not "
            "show both feedbacks done with uses = 1. The counterexample is not "
            "the lost update this check is about.\n" + bad_out
        )
    print("tlc broken: NoLostFeedbackUpdate violated, as required (uses = 1 after both feedbacks)")
    print("--- broken trace (read it with /statechart) ---")
    print(bad_out.rstrip())
    print("--- end trace ---")


def _print_broken_trace() -> None:
    """The find-outside model, so a PROPERTY FAIL is paired with the TLC path."""
    try:
        bad_code, bad_out = _run_tlc("FeedbackWriteLockBroken.cfg")
    except CheckError as exc:
        print(str(exc), file=sys.stderr)
        return
    if bad_code != 0 and "Invariant NoLostFeedbackUpdate is violated" in bad_out:
        print("--- broken trace (read it with /statechart) ---", file=sys.stderr)
        print(bad_out.rstrip(), file=sys.stderr)
        print("--- end trace ---", file=sys.stderr)
    else:
        print(
            "BROKEN-MODEL FAIL: could not show the find-outside trace "
            f"(exit {bad_code}).",
            file=sys.stderr,
        )
        print(bad_out, file=sys.stderr)


def main() -> int:
    try:
        check_sync_commit()
        check_feedback_correspondence()
        check_write_lock_correspondence()
        print("correspondence: feedback's find / _record_event / _save are inside one write_lock; lock order matches")
        check_runtime()
        check_tlc()
    except CheckError as exc:
        print(str(exc), file=sys.stderr)
        text = str(exc)
        if "PROPERTY FAIL" in text and "find" in text and "outside" in text:
            _print_broken_trace()
        return 1
    print("PASS: NoLostFeedbackUpdate holds for the modeled interleavings of the current feedback write_lock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
