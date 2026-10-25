---
name: verify-feedback-write-lock
description: >
  Re-check the proved promise that two feedbacks on one memory cannot lose a
  uses update (the same _save also writes confidence). Use when a change
  touches lifecycle.feedback, WriteLocker.write_lock, MemoryStore.find's
  snapshot, or verification/feedback-write-lock, and before moving sync_commit.
---

# Verify feedback's write lock

Two `feedback` calls on one memory must not lose a `uses` update. `find` reads a snapshot, `_record_event` adds one use on that object, `fold_success` / `fold_failure` adjust confidence on that same object, and `_save` writes the object back. If both calls read the same snapshot, the later `_save` puts the earlier increment back, and the confidence that rode on it goes back too. Agents then rank and decay from a count that never happened.

The harm is silent: the caller still gets a successful feedback, and the file looks well formed. Thread tests miss it when the critical section is short (`tests/test_concurrency.py` says a pure schedule race under-reports). This check explores every interleaving of the steps below, and it runs the real `feedback` once to see that `find` and `_save` hold the lock.

## When a change touches this

Re-run the check if the diff touches any of these. A change that does not touch them is outside this proof.

- `src/compound_memory/storage/lifecycle.py` — `feedback`, `_record_event`, `fold_success`, `fold_failure`
- `src/compound_memory/storage/locking.py` — `WriteLocker.write_lock` (mutex, `_local.lock_depth`, `_lock_exclusive`, yield, unlock, release)
- `src/compound_memory/storage/files.py` — `find` (each call parses a new `Memory`; the model treats that as the snapshot)
- `src/compound_memory/storage/facade.py` — `MemoryStore._write_lock`, `MemoryStore._write_lock_depth` (the runtime probe reads the depth)
- `verification/feedback-write-lock/` — the TLA+ model, either config, or `check.py`
- this skill

`write`, `distill_apply`, `review_resolve`, `decay_sweep`, `revive`, and `forget` use the same lock and are not in the model. Editing only those does not re-prove them. If you move their `find` outside the lock, this command stays green.

## Re-run

From the repo root, with Java 11+ on `PATH` (this check pins TLC 2.19, tlaplus 1.7.4):

```bash
uv run python verification/feedback-write-lock/check.py
```

Exit 0 is a pass. Anything else is a fail. The first run downloads `tla2tools-1.7.4.jar` into `verification/feedback-write-lock/.cache/` (gitignored) and checks its sha256. The checker puts `src/` first on `sys.path`, so it imports the working tree, not an older installed wheel.

Read every TLC result with pstack `/statechart`. On a violation it shows the shortest path in plain words. On a pass it draws how the two feedbacks move. Refer to that skill by name only.

## What a failure means

The command prints one of these labels. Trust the label before editing the product or the property.

- **PROPERTY FAIL** — the working tree broke the promise. Either `feedback` called `find` or `_save` while `_write_lock_depth` was 0, or `write_lock` reads depth before `self._mutex.acquire`, or it yields after `self._mutex.release`, or the reentry test is no longer `self._local.lock_depth`. That is a real bug. The trace from `FeedbackWriteLockBroken.cfg` (when the failure is `find` outside the `with`) is the same bug in the model's words: writer `a` reads `uses = 0` before the lock, writer `b` reads `uses = 0` before `a` has `_save`d, both write 1, and both reach `done` with `uses = 1`. Replay that against `MemoryStore.feedback` if you need a file on disk. Do not weaken `NoLostFeedbackUpdate`. Do not set `ReadOutsideLock = FALSE` in the broken config.
- **CORRESPONDENCE FAIL** — the source no longer has the steps the model names (one `with store._write_lock()` in `feedback`; `find`, `_record_event`, `fold_success`, `fold_failure`, and `_save` in that body and not outside it; `write_lock` still has acquire, one thread-local depth test, `_lock_exclusive`, yield, release). Read the diff. Renaming a function, or splitting the `with`, is a modeling change: retarget the TLA actions and the AST names in `check.py` to the new code, then re-run. If the new order lets `find` happen before the lock, or lets the other thread take the reentry path, update the model to that order and TLC will violate `NoLostFeedbackUpdate`. That violation is a real bug. Leave the property.
- **MODEL FAIL** — correspondence and the runtime probe passed, but TLC on `FeedbackWriteLock.cfg` did not. The model disagrees with itself or with TLC. Fix the model, not product code, unless you replay the trace through `feedback` and watch `uses` drop. A state graph shallower than 20, or fewer than 40 distinct states, is a vacuous model, not a proof.
- **BROKEN-MODEL FAIL** — `ReadOutsideLock = TRUE` did not violate `NoLostFeedbackUpdate`. The check can no longer fail. Restore a broken variant that loses the update. Do not report a pass.
- **TOOL FAIL** — Java missing, the jar sha256 did not match, or the download failed. Not a product bug.

A note that covered files differ from `sync_commit` is not itself a failure. The check still runs on the working tree.

## When the change is intentional

1. Read the new `feedback` and `write_lock` and list the steps that are still one indivisible action. Update the action comments in `FeedbackWriteLock.tla` (file and line) so a reviewer can open them. An action that is not one step in the code is a modeling error.
2. Keep `NoLostFeedbackUpdate` as: both writers at `pc = "done"` implies `uses = 2`, starting from `uses = 0`, one feedback each. That sentence is the promise. Do not change it to match a weaker code path.
3. If you renamed a call the AST checker looks for, update `INSIDE_CALLS` / the lock-step checks in `check.py` in the same commit. The names must be the real functions.
4. Re-run the command. The correct config must pass and the broken config must still violate `NoLostFeedbackUpdate`.
5. Only then set `sync_commit` to the new commit SHA, in both this skill and the header of `FeedbackWriteLock.tla`. The checker requires those two lines to be the same single SHA. Advancing the SHA without a passing run records a proof you did not re-check.

## Coverage

sync_commit: b2fe04c36b36662d05bf8d28a7988726bded8a25

Confirmed against that commit: the covered product files above match the model, `check.py` passed, and the broken config violated `NoLostFeedbackUpdate`. Product code was not changed to get the pass. Advance this SHA only after step 5.

| Model action | Code |
| --- | --- |
| `Begin` | `lifecycle.feedback` enters `with store._write_lock()` |
| `FindOutside` | `lifecycle.py` `store.find` placed before that `with` (broken config only) |
| `AcquireMutex` | `locking.py` `WriteLocker.write_lock` `self._mutex.acquire()` |
| `CheckDepth` | same function, `if self._local.lock_depth > 0` |
| `TakeFlock` | same function, `_lock_exclusive` |
| `Enter` | same function, `self._local.lock_depth += 1` |
| `FindInside` | `lifecycle.py` `feedback`, `mem = store.find(mem_id)` inside the `with` |
| `Bump` | `lifecycle.py` `_record_event`, `mem.uses += 1` |
| `Save` | `lifecycle.py` `feedback`, `store._save(mem)` |
| `Exit` | `locking.py` `write_lock`, depth `-= 1` and `_unlock_fd` when this acquire took the flock |
| `ReleaseMutex` | `locking.py` `write_lock`, `self._mutex.release()` |

`files.find` is the snapshot `FindInside` / `FindOutside` reads. `facade._write_lock` is the delegate the runtime probe enters; `facade._write_lock_depth` is what it asserts.

## Bounds

Proved only for this model, not for the whole store:

- Two threads of one process, one memory, each runs one feedback that finds the memory. `uses` starts at 0. The promise is `uses = 2` after both have saved.
- `ReadOutsideLock = FALSE` matches the current `with`. The other config is the bug, and it must keep failing.
- The lock file is created and the flock is acquired. The `os.open` failure path in `write_lock` (warn and continue without a flock, mutex still held) is not in the model. Same-process threads are still excluded by the mutex on that path; other processes are not.
- `WriteLockError` (lock not acquired) refuses the write. The model has no feedback that saves after that error.
- Same-thread reentry (`batch` already holds the lock, depth `> 0`, skip flock) is a branch in `CheckDepth` and is unreachable in this run: each thread checks depth before `Enter` increments it. The AST check still requires the test to be `self._local.lock_depth` and to sit after `acquire`. It does not explore a nested `feedback` inside `batch`.
- Cross-process exclusion is the flock step, but a third process is not a state. Two threads already contend on that flock.
- Git commit, index sync, and the confidence arithmetic are not separate schedules. Confidence is lost exactly when this `uses` update is lost, because `_save` writes one `Memory`. The model does not check the `+0.1` / `+0.15` / `host_bumps` table.
- `obsolete` archives inside the same `with`. The runtime probe runs the non-obsolete success path only. The AST check still requires `_save` in that `with`, which is also the already-archived `obsolete` arm.
- Scheduling is the interleaving of the named actions. A step is atomic to the same extent the Python statement is: `find` returns one parsed `Memory`, `_save` replaces the file.

Not covered, and the closest next targets if you extend this: `distill_apply` reads sources inside `batch()` (which holds this lock) and archives them — the same lost update if that `find` moves outside the batch; and ADR-0012 `host_bumps` together with `resolve_identity`, where one attested process must not mint a second host key and collect another cross-host `+0.15` or another `0.3` cap. That second one is every sequence of outcomes and claimed ids on the real `feedback` function, not another lock order.

To watch the property fail on purpose: move `mem = store.find(mem_id)` in `feedback` to above the `with`, re-run, expect `PROPERTY FAIL`, then put the line back. Do not commit that move.
