# Changelog

## Unreleased

- New memory ids are `YYYYMMDD_` plus the full uuid4 hex (32 characters). Allocation runs under the write lock and retries when that id already names a file in the active or archive tree, so a collision never overwrites and cannot alias `find` or the vector index. Existing `YYYYMMDD_` + 6 hex files still read (#109).
- Writes reject a `confidence` that is a bool, a non-finite float, or outside `[0, 1]` with `ValueError` before creating a file. `0.0`, `1.0`, and ordinary floats still store. CLI `distill-apply` and internal writes share this check. A leading minus is not a new flag: `--confidence -inf` is parsed as a number (same as `--confidence=-inf`) and then rejected. MCP `memory_write` does not gain a confidence parameter (#110).
- The same-host success-cap decision is ADR-0012 (`docs/adr/0012-same-host-success-cap.md`). ADR-0011 is only the dream `--propose` pack. Spec, README, and the feedback contract text now name `host_bumps` and the 0.3 same-host cap (#111).
- Same `MemoryStore` write lock no longer treats another thread's reentry depth as its own. A process-local mutex is taken first, and lock/batch depth is thread-local, so concurrent feedback on one store cannot drop `uses` (#107).
- Extraction drops password/account phrases from fact candidates and redacts password/token/key-shaped text before writing `extract/last-candidates.json`. Sentence splits use each punctuation mark, so one turn can yield multiple candidates (#107). Search and read paths are unchanged.
- WorkBuddy/CodeBuddy and ZCode marketplace plugins bumped to 0.1.2 for compound-memory 0.5.10 alignment. Outside the PyPI sdist and wheel.
- Claude Code marketplace plugin bumped to 0.1.3 so marketplace clients refresh against compound-memory 0.5.10 (CLI `maintain`). The bundled skill stays the thin wrapper, and deep rules stay at the canonical skill URL. Outside the PyPI sdist and wheel.

## 0.5.10 - 2026-10-22

- CLI `maintain` is one daily pass: read-only `dream` (writes `<root>/distill/last-plan.json`, no `--propose`), then optional `decay`, then `health-snapshot`. Decay stays off unless `--with-decay` or `RUN_DECAY=1` (`DECAY=1` alias). A failing step exits non-zero and names that step. `scripts/daily-maintain.sh` is a thin optional wrapper that only resolves uv/PATH and execs this command. `scripts/distill-prepare.sh` stays dream-only. `--propose`, `dream-apply`, and `distill-apply` stay out of maintain and out of timers (ADR-0001 / #72 / #97). No new MCP tool (#102).

## 0.5.9 - 2026-10-18

- CLI `dream --propose` writes `<root>/distill/dream-proposals.json` in the existing `dream-apply` JSON-list shape (`content`, `type`, `source_ids`, `key`). Plain `dream` stays read-only with the same stdout and no proposals file. The gate is ADR-0011: same type and key, at least 3 memories, pairwise content token coverage at least 0.5, evidence total at least 3, and no pending contradiction. When nothing qualifies, the report still prints and the file is `[]`. Schedulers keep calling `dream` without `--propose`. No new MCP tool (#98 / #97).
- Same-host success confidence bumps are capped at +0.3 for a given host via the evidence `host_bumps` quota (ADR-0012 same-host success cap; the file was briefly numbered 0011 alongside dream `--propose`). The ADR is recorded and the Confidence glossary entry is synced (#99).
- CLI `health-snapshot` appends today's `stats` payload to `<root>/health/health.jsonl`. CLI `health-trend` summarizes the window (last row per day, plus a first-vs-last delta). Both are CLI-only. No new MCP tool (#100).
- Docs: `docs/eval/` is a checklist for reproducible A/B notes. "Can run" is `doctor` exit 0, one call to each of the five MCP tools, and host install smoke pointers (Claude, WorkBuddy, DeepSeek Harness). "Is effective" is a fixed 8-prompt set plus a result template. `uses`, `cross_validated`, and `recent_feedback_7d` are documented as health, not proof; `failure` and `contradiction` have to be recorded. `scripts/eval_stats_snapshot.py` saves CLI `stats` JSON. No new MCP tool, no in-process LLM, and no claim that effectiveness is already measured.
- `scripts/validate_plugins.py` (CI, ubuntu × 3.13): mechanical checks for the marketplace/plugin files that no test covered — catalog ↔ manifest name/version agreement (the #92 drift), source directories, component paths inside the plugin root, hook command placeholder plus referenced script existence and executable bit, and no repo-root `.mcp.json`.
- Host plugin integration guide `docs/agents/host-plugin-integration.md`: which catalog file each host's loader reads (ZCode checks `.claude-plugin/marketplace.json` before a root-level one) and the sync checklist for adding a host plugin.
- ZCode marketplace plugin under `integrations/zcode` (`COMPOUND_MEMORY_AGENT_ID=agent-zcode`). ZCode reads the repository-root `.claude-plugin/marketplace.json` as its marketplace catalog, so the `compound-memory-zcode` entry is added there. Not shipped in the PyPI sdist or wheel.
- WorkBuddy / CodeBuddy marketplace plugin under `integrations/workbuddy` (`COMPOUND_MEMORY_AGENT_ID=agent-workbuddy`). Not shipped in the PyPI sdist or wheel.
- Claude Code marketplace plugin under `integrations/claude` (not shipped in the PyPI sdist or wheel).
- Claude Code plugin SessionStart hook (`startup`) injects a short hard-rule reminder. Still outside the PyPI sdist and wheel.
- Host plugin package versions bumped (Claude 0.1.2, WorkBuddy 0.1.1, ZCode 0.1.1). They remain outside the PyPI sdist and wheel.

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
