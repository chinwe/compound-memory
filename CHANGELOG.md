# Changelog

## Unreleased

## 0.5.5 - 2026-10-13

- CLI `dream-apply` applies caller-supplied products (`content`, `type`, `source_ids`) through existing `distill_apply` when the signal is `merge_with`, `key_duplicates`, or an explicitly opted-in `promotion_candidate`. `possible_dup_of` stays report-only. `dream` stays read-only, and timers still call only `dream` (#72).

## 0.5.4 - 2026-10-12

- CLI `dream` prints a read-only offline consolidation report (`distill_plan`, decay signals, and stats). It does not auto-write, call an LLM, or add an MCP tool (#70 / #69).

## 0.5.3 - 2026-10-12

- Retrieval visibility is unified behind `is_retrievable`, so search and the CLI apply the same archived, expiry, namespace, and project filter (#67).

## 0.5.2 - 2026-10-11

- Docs: spec write-lock text matches POSIX `flock` vs Windows `msvcrt` (`WriteLockError` on timeout; unlocked fallback only when the lock file cannot be created). Conflict handling points at `review-resolve` instead of hand-moving files. Feedback docs state that every outcome advances `uses` / `last_used`. `stats.cross_validated` is memories with at least two `validated_by` agents.
- Host docs recommend `uvx --from compound-memory compound-memory-server` for MCP clients. `uv run --directory <repo>` is only the local-checkout path.

## 0.5.1 - 2026-10-11

- Host docs treat `COMPOUND_MEMORY_AGENT_ID` as required. `stats.attestation` and the MCP server startup log say when it is unset, because self-reported identity can be forged (#57).
- Windows CI matches Linux (Python 3.11, 3.12, and 3.13) and runs the wheel smoke test. A write-lock timeout or lock-syscall failure refuses the write instead of continuing unlocked. Creating the lock file can still fall open with a warning (#58).
- Docs: architecture tree includes `decision` and `index/vectors.db`; the compounding table matches feedback outcomes; Cursor has a setup snippet; Claude Code status is ✅ in both places; LongMemEval ingest describes `store.batch()` (#59).
- `stats.retrieval` and MCP `memory_search` report `vector` or `lexical` (with a reason). `memory_write` rejects content over 262144 characters and does not create a file (#60).
- `COMPOUND_MEMORY_ROOT` expands a leading `~` or `$HOME` (#61).

## 0.5.0

- First release with Windows support: file locking uses `msvcrt` on Windows and `flock` on POSIX; atomic writes skip `fchmod` where it does not exist. Classifiers declare Windows. CI gained `windows-latest` (Python 3.13 in that release; the 3.11/3.12 Windows jobs are unreleased follow-up) (#55).
- `__version__` follows the packaged release (it had been stuck at 0.2.0). CLI `--version` matches the tag.
- MCP Registry `server.json` version tracks the PyPI release.

Older tags (`v0.4.3` and below) are on the GitHub releases page.
