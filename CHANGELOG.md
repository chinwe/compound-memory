# Changelog

## Unreleased

## 0.5.8 - 2026-10-16

- CLI `stats` and `doctor` exit 1 when `COMPOUND_MEMORY_AGENT_ID` is unset or blank (the diagnosis is still printed). `doctor` reports attestation, retrieval mode, write lock, and git. MCP tools are unchanged and do not fail a session for a missing agent id (#84).
- Docs: the English host guide lists `doctor`'s full exit-1 surface (unset or blank agent id, missing or non-directory root, write lock that cannot be acquired, git missing / not a repository / `status` failed). Lexical-only retrieval and a dirty worktree are reported and do not fail. A doctor `write_lock` failure is the probe; lock-file creation failure on the write path still warns and may write unlocked (#88).
- mypy checks `storage.validation`, `storage.paths`, and `server` with per-module strict flags. The rest of the package stays non-strict (#83).
- English slim host guide: `docs/agent-integration.en.md` (uvx install, required `COMPOUND_MEMORY_AGENT_ID`, the five MCP tools, search → feedback → write). `docs/agent-integration.md` remains the full Chinese guide (#83).

## 0.5.7 - 2026-10-15

- Lesson and avoid semantics: `lesson_role` (`lesson` or `avoid`) on an existing type, and link prefixes `avoid:`, `fixes:`, and `contradicts:`. `distill-plan` and `dream` report `negative_salience` and `contradiction_pairs`. `dream-apply` is not widened, and there is no new MCP tool or in-process LLM (#78 / #79).

## 0.5.6 - 2026-10-15

- `distill-plan` and `dream` annotate `suggested_action` as `merge`, `promote`, or `review`. Same-key groups are auto-listed as merge clusters (`source_ids`); cross-key weak duplicates stay `review` and `dream-apply` does not admit them. No new MCP tool, no in-process LLM, and timers still call only `dream` (#75 / #76).

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

History before published GitHub Releases lives in this changelog and in git tags. Release notes from `v0.5.7` onward are on the [GitHub Releases](https://github.com/chinwe/compound-memory/releases) page.
