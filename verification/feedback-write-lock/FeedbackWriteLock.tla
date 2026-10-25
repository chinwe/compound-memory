------------------------------ MODULE FeedbackWriteLock ------------------------------
\* sync_commit: b2fe04c36b36662d05bf8d28a7988726bded8a25
\*
\* NoLostFeedbackUpdate — two feedbacks on one memory do not lose a uses update.
\*
\* The stored uses count (and the confidence on that same Memory snapshot) is
\* what a later reader sees. A feedback reads the snapshot in find, adds one
\* use on that object, and writes the object back in _save. If both feedbacks
\* read the same snapshot, the later _save puts the earlier increment back.
\*
\* Code each action stands for (one action = one indivisible step there):
\*   Begin          lifecycle.py feedback, the step into the with
\*                  (the with itself is locking.py WriteLocker.write_lock)
\*   FindOutside    lifecycle.py:182 store.find, placed BEFORE the with.
\*                  Only when ReadOutsideLock. That is the lost-update bug
\*                  (2026-10-05: find outside the lock, 13/16 runs dropped uses).
\*   AcquireMutex   locking.py:225 self._mutex.acquire()
\*                  Held across the whole critical section, including flock,
\*                  so a waiter cannot sit on flock while the holder needs
\*                  the mutex to unlock (deadlock note at locking.py:223).
\*   CheckDepth     locking.py:227 if self._local.lock_depth > 0
\*                  Thread-local. The other writer's depth does not count.
\*                  TRUE skips flock (same-thread reentry, batch). This run
\*                  never takes TRUE: each writer starts at depth 0 and
\*                  checks depth before Enter increments it. Batch reentry
\*                  is not explored. See the skill for that bound.
\*   TakeFlock      locking.py:243 _lock_exclusive(fd)
\*                  Blocks while the other writer holds the .lock flock.
\*   Enter          locking.py:250 self._local.lock_depth += 1
\*                  (reentry arm is locking.py:228, same increment)
\*   FindInside     lifecycle.py:182 mem = store.find(mem_id)
\*                  Reads mem.uses. Inside the with when ReadOutsideLock = FALSE.
\*   Bump           lifecycle.py:141 mem.uses += 1 inside _record_event
\*                  The local Memory only. The file is unchanged until _save.
\*                  Confidence is folded on this same object (fold_success /
\*                  fold_failure) before _save; it is not a separate write.
\*   Save           lifecycle.py:232 store._save(mem)
\*                  Writes the whole snapshot, uses and confidence together.
\*                  Obsolete's _archive / already-archived _save are the same
\*                  kind of write and are inside the same with; this run is
\*                  the non-obsolete success path.
\*   Exit           locking.py:254 depth -= 1, then locking.py:257 _unlock_fd
\*                  when this acquire took the flock.
\*   ReleaseMutex   locking.py:261 self._mutex.release()
\*   Stop           not code. Keeps the terminal state from looking deadlocked
\*                  once both feedbacks have returned.
\*
\* Identity resolution (lifecycle.py:178 _resolve_identity) is before the with
\* and does not read the memory. It is not a step here.
\*
\* Two threads, one MemoryStore, one memory, one feedback each. uses starts at 0.
\* ReadOutsideLock = FALSE is write_lock's with wrapped around find (the code).
\* ReadOutsideLock = TRUE is find before that with (the bug).

EXTENDS Integers

CONSTANT ReadOutsideLock

VARIABLES pc, snapshot, uses, mutex, flock, depth, phase

Writers == {"a", "b"}

vars == <<pc, snapshot, uses, mutex, flock, depth, phase>>

Init ==
  /\ pc = [w \in Writers |-> "go"]
  /\ snapshot = [w \in Writers |-> -1]
  /\ uses = 0
  /\ mutex = "free"
  /\ flock = "free"
  /\ depth = [w \in Writers |-> 0]
  /\ phase = FALSE

Begin(w) ==
  /\ ~ReadOutsideLock
  /\ pc[w] = "go"
  /\ pc' = [pc EXCEPT ![w] = "acq"]
  /\ UNCHANGED <<snapshot, uses, mutex, flock, depth, phase>>

FindOutside(w) ==
  /\ ReadOutsideLock
  /\ pc[w] = "go"
  /\ snapshot' = [snapshot EXCEPT ![w] = uses]
  /\ pc' = [pc EXCEPT ![w] = "acq"]
  /\ UNCHANGED <<uses, mutex, flock, depth, phase>>

AcquireMutex(w) ==
  /\ pc[w] = "acq"
  /\ mutex = "free"
  /\ mutex' = w
  /\ pc' = [pc EXCEPT ![w] = "depth"]
  /\ UNCHANGED <<snapshot, uses, flock, depth, phase>>

CheckDepth(w) ==
  /\ pc[w] = "depth"
  /\ IF depth[w] > 0
     THEN pc' = [pc EXCEPT ![w] = "enter"]
     ELSE pc' = [pc EXCEPT ![w] = "flock"]
  /\ UNCHANGED <<snapshot, uses, mutex, flock, depth, phase>>

TakeFlock(w) ==
  /\ pc[w] = "flock"
  /\ flock = "free"
  /\ flock' = w
  /\ pc' = [pc EXCEPT ![w] = "enter"]
  /\ UNCHANGED <<snapshot, uses, mutex, depth, phase>>

Enter(w) ==
  /\ pc[w] = "enter"
  /\ depth' = [depth EXCEPT ![w] = depth[w] + 1]
  /\ pc' = [pc EXCEPT ![w] = IF ReadOutsideLock THEN "bump" ELSE "find"]
  /\ UNCHANGED <<snapshot, uses, mutex, flock, phase>>

FindInside(w) ==
  /\ pc[w] = "find"
  /\ snapshot' = [snapshot EXCEPT ![w] = uses]
  /\ pc' = [pc EXCEPT ![w] = "bump"]
  /\ UNCHANGED <<uses, mutex, flock, depth, phase>>

Bump(w) ==
  /\ pc[w] = "bump"
  /\ snapshot[w] >= 0
  /\ snapshot' = [snapshot EXCEPT ![w] = snapshot[w] + 1]
  /\ pc' = [pc EXCEPT ![w] = "save"]
  /\ UNCHANGED <<uses, mutex, flock, depth, phase>>

Save(w) ==
  /\ pc[w] = "save"
  /\ uses' = snapshot[w]
  /\ pc' = [pc EXCEPT ![w] = "exit"]
  /\ UNCHANGED <<snapshot, mutex, flock, depth, phase>>

Exit(w) ==
  /\ pc[w] = "exit"
  /\ depth[w] >= 1
  /\ depth' = [depth EXCEPT ![w] = depth[w] - 1]
  /\ IF flock = w THEN flock' = "free" ELSE UNCHANGED flock
  /\ pc' = [pc EXCEPT ![w] = "rel"]
  /\ UNCHANGED <<snapshot, uses, mutex, phase>>

ReleaseMutex(w) ==
  /\ pc[w] = "rel"
  /\ mutex = w
  /\ mutex' = "free"
  /\ pc' = [pc EXCEPT ![w] = "done"]
  /\ UNCHANGED <<snapshot, uses, flock, depth, phase>>

\* Both feedbacks have returned. Flip a bit so TLC still has a successor.
Stop ==
  /\ \A w \in Writers : pc[w] = "done"
  /\ phase' = ~phase
  /\ UNCHANGED <<pc, snapshot, uses, mutex, flock, depth>>

Next ==
  \/ \E w \in Writers :
       \/ Begin(w)
       \/ FindOutside(w)
       \/ AcquireMutex(w)
       \/ CheckDepth(w)
       \/ TakeFlock(w)
       \/ Enter(w)
       \/ FindInside(w)
       \/ Bump(w)
       \/ Save(w)
       \/ Exit(w)
       \/ ReleaseMutex(w)
  \/ Stop

Spec == Init /\ [][Next]_vars

\* Both committed feedbacks are visible: uses went 0 → 1 → 2, not back to 1.
\* Vacuous until both pc values are "done". Stop does not change uses or pc.
NoLostFeedbackUpdate ==
  (pc["a"] = "done" /\ pc["b"] = "done") => (uses = 2)

TypeOK ==
  /\ pc \in [Writers -> {
       "go", "acq", "depth", "flock", "enter", "find", "bump", "save", "exit", "rel", "done"}]
  /\ \A w \in Writers : snapshot[w] \in -1..2 /\ depth[w] \in 0..1
  /\ uses \in 0..2
  /\ mutex \in (Writers \cup {"free"})
  /\ flock \in (Writers \cup {"free"})
  /\ phase \in BOOLEAN

=============================================================================
