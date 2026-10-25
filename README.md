# compound-memory

[![CI](https://github.com/chinwe/compound-memory/actions/workflows/ci.yml/badge.svg)](https://github.com/chinwe/compound-memory/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/compound-memory)](https://pypi.org/project/compound-memory/)
[![Python](https://img.shields.io/pypi/pyversions/compound-memory)](https://pypi.org/project/compound-memory/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

English | [简体中文](README.zh-CN.md)

Local-first shared memory for multiple AI agents — plain Markdown files that **compound in value as they are used**. Memory lives on your disk as frontmatter-annotated Markdown, gets stronger with every confirmed use, decays into a revivable archive when neglected, and auto-commits to a local git history on every write.

## Why

Every agent session starts from zero: preferences get re-asked, project conventions get re-discovered, the same pitfall gets hit twice. compound-memory gives all your agents one shared store:

- **Local-first** — nothing leaves your machine; memories are human-readable Markdown files, not rows in an opaque database.
- **MCP-native** — exactly 5 tools (`memory_write` / `memory_search` / `memory_get` / `memory_link` / `memory_feedback`) as the single read-write boundary; works with any MCP host (Claude Code, ZCode, WorkBuddy, …), plus a full CLI for operations.
- **Compounding** — confirmed usage raises confidence, related memories are recalled as neighbors, validation from a *different* host counts as independent evidence, and distillation merges many raw memories into fewer, denser ones.
- **Multi-agent by design** — a `_shared` namespace everyone reads, plus `agent-*` private namespaces each host owns; cross-host validation is tracked per host.
- **Optional semantic recall** — vector search via sqlite-vec + BGE embeddings, with automatic graceful fallback to pure lexical search when unavailable. Override the model with `COMPOUND_MEMORY_EMBEDDING_MODEL` (default `Xenova/bge-small-zh-v1.5`) and `COMPOUND_MEMORY_EMBEDDING_DIM` (default 512); changing either is an ops action and needs an explicit `rebuild-index`.

## Quick Start

### For AI agents

Paste this one-liner into your coding agent (Claude Code, Cursor, ZCode, …) and let it do the rest:

```text
Set up compound-memory (https://github.com/chinwe/compound-memory) — a local-first multi-agent shared memory (MCP server + CLI) — on this machine. Do not clone the repo for a normal install. Initialize the store with `uvx --from compound-memory compound-memory init` (defaults to ~/.agents/memory). Register its stdio MCP server in this host's MCP config: command `uvx`, args `--from compound-memory compound-memory-server`, env `COMPOUND_MEMORY_ROOT=$HOME/.agents/memory` and `COMPOUND_MEMORY_AGENT_ID=agent-<your-host-id>`. Verify by calling `memory_search` and expecting a `{"hits": [...]}` response; if the host needs a restart to load MCP servers, tell me. Use `uv run --directory <repo>` only when developing against a local checkout. Short English setup and the search → feedback → write loop: docs/agent-integration.en.md. Full per-host configs: docs/agent-integration.md.
```

### For humans

#### 1. Install

Python ≥ 3.11. The default does not clone the repo:

```bash
# Default: PyPI. Hosts launch the MCP server with
#   uvx --from compound-memory compound-memory-server
# Optional CLI on PATH:
uv tool install compound-memory   # or: pip install compound-memory

# Developer / local checkout only — not the default host launch
git clone https://github.com/chinwe/compound-memory.git
cd compound-memory && uv sync --extra dev
```

#### 2. Initialize your store

Defaults to `~/.agents/memory`; override with the `COMPOUND_MEMORY_ROOT` env var. A leading `~` or `$HOME` is expanded; a blank override falls back to the default.

```bash
uvx --from compound-memory compound-memory init
# after `uv tool install`: compound-memory init
```

#### 3. Wire it into your MCP host (recommended)

This lets your everyday agents read/write the shared store automatically. No repo path:

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "compound-memory", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-<your-host-id>"
      }
    }
  }
}
```

`uvx` takes the package name; the server console script is `compound-memory-server`, so `--from` is required. If `uvx` is not on `PATH`, set `command` to the absolute path `~/.local/bin/uvx`. `COMPOUND_MEMORY_AGENT_ID` is **required** for any multi-agent store: the process identity wins over whatever the model claims, so a forged `source` cannot write another host's namespace or inflate cross-host validation. Unset or blank is a hard failure for shared `_shared` use: `stats` and `doctor` exit 1 (the diagnosis is still printed). MCP tools keep running, and the server logs the same warning at startup. Without the id, identity is self-reported and can be forged.

A local checkout is the developer path, not a second default: `command` `uv`, `args` `["run", "--directory", "<absolute-repo-path>", "compound-memory-server"]` after `uv sync --extra dev`.

**Verify**: ask your agent to call `memory_search` (any keyword) — a `{"hits": [...]}` response means you're connected. Or run `uvx --from compound-memory compound-memory doctor` (exit 0 when `COMPOUND_MEMORY_AGENT_ID` is set and the store is writable; exit 1 when it is unset or blank).

#### Claude Code plugin

Requires Claude Code and `uv` (`uvx` on `PATH`). This repository hosts a self-hosted marketplace. Submitting the plugin to Anthropic's official Directory is out of scope.

```bash
claude plugin marketplace add chinwe/compound-memory
claude plugin install compound-memory@compound-memory
```

That installs [`integrations/claude`](integrations/claude/README.md), which launches `uvx --from compound-memory compound-memory-server` with `COMPOUND_MEMORY_AGENT_ID=agent-claude` (override that env var in the host if you need a different id). A SessionStart hook adds a short reminder on new sessions; verify and disable steps are in the plugin README. Those marketplace and plugin files are not in the PyPI sdist or wheel: `MANIFEST.in` prunes them, and `scripts/wheel_smoke.sh` checks the built artifacts.

#### WorkBuddy / CodeBuddy plugin

Requires WorkBuddy or CodeBuddy and `uv` (`uvx` on `PATH`). This repository hosts a self-hosted marketplace at `.codebuddy-plugin/marketplace.json`.

```text
/plugin marketplace add chinwe/compound-memory
/plugin install compound-memory@compound-memory
```

That installs [`integrations/workbuddy`](integrations/workbuddy/README.md), which launches the same PyPI server with `COMPOUND_MEMORY_AGENT_ID=agent-workbuddy`. A SessionStart hook prints a short hard-rule reminder on new sessions and does not read the store. The Claude marketplace file is unchanged.

#### ZCode plugin

Requires the ZCode desktop app and `uv` (`uvx` on `PATH`). ZCode reads this repository's `.claude-plugin/marketplace.json` as its marketplace catalog, so no separate marketplace file is needed.

Clone the repository, then in ZCode: **Plugin Marketplace → Add → Add Plugin Marketplace**, paste the clone path (the directory containing `.claude-plugin/`), and install **Compound Memory (ZCode)** under **Personal → compound-memory**. That installs [`integrations/zcode`](integrations/zcode/README.md), which launches the same PyPI server with `COMPOUND_MEMORY_AGENT_ID=agent-zcode`, plus a SessionStart hard-rule reminder and the short skill. The plugin's MCP server is named `compound-memory`; if `~/.zcode/cli/config.json` already registers a server with that name (the hand-written path, §3 of [`docs/agent-integration.md`](docs/agent-integration.md)), keep only one of the two.

#### 4. Next step

Inject the usage protocol from [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md) into your host (the search → feedback → write loop). The short English version is [`docs/agent-integration.en.md`](docs/agent-integration.en.md); the full protocol is `docs/agent-integration.md` §6.

## Demo

![compound-memory CLI demo: init → write → search → feedback → stats](https://raw.githubusercontent.com/chinwe/compound-memory/main/assets/demo.gif)

One full loop: write → search → feedback (with cross-host first-validation bonus) → store health. Real output from v0.4.0, long payloads trimmed:

```bash
uv run compound-memory init
```

```json
{ "ok": true, "root": "~/.agents/memory" }
```

Two different hosts each write one stable fact (new memories start at `confidence` 0.5, `uses` 0):

```bash
uv run compound-memory write \
  "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget" \
  fact agent-claude --key vercel-timeout
```

```json
{
  "id": "20261007_86adf1",
  "ns": "_shared",
  "type": "fact",
  "source": "agent-claude",
  "content": "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget",
  "confidence": 0.5,
  "uses": 0,
  "key": "vercel-timeout",
  "validated_by": []
  ...
}
```

```bash
uv run compound-memory write \
  "User prefers concise replies with tables and code examples" \
  fact agent-zcode --key user-style
```

Search ranks by score (`--explain` attaches per-hit ranking components for debugging):

```bash
uv run compound-memory search "serverless timeout"
```

```json
[
  {
    "id": "20261007_86adf1", "score": 1.0292, "similarity": 1.0,
    "type": "fact", "source": "agent-claude",
    "content": "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget",
    "neighbors": []
  },
  {
    "id": "20261007_6a0c0c", "score": 0.5211, "similarity": 0.4919,
    "type": "fact", "source": "agent-zcode",
    "content": "User prefers concise replies with tables and code examples",
    "neighbors": []
  }
]
```

A different host used this memory and reported it back with the default `success` outcome — `uses` +1, `conf` +0.1; and since the reporter `agent-workbuddy` ≠ source `agent-claude`, the first cross-host validation adds another +0.15:

```bash
uv run compound-memory feedback 20261007_86adf1 agent-workbuddy
```

```json
{
  "id": "20261007_86adf1",
  "confidence": 0.75,
  "uses": 1,
  "last_used": "2026-10-07",
  "validated_by": ["agent-workbuddy"],
  "evidence": {
    "success_count": 1, "failure_count": 0, "contradiction_count": 0,
    "last_verified": "2026-10-07",
    "recent": [{ "date": "2026-10-07", "agent": "agent-workbuddy", "outcome": "success" }]
  }
  ...
}
```

Store health at a glance (fixed-bucket histograms, liveness, distillation yield):

```bash
uv run compound-memory stats
```

```json
{
  "total": 2, "archived": 0, "active": 2,
  "avg_confidence": 0.625,
  "by_type": { "fact": 2 },
  "by_ns": { "_shared": 2 },
  "review_queue_entries": 0,
  "uses_histogram": { "0": 1, "1-2": 1, "3-5": 0, "6-9": 0, "10+": 0 },
  "confidence_histogram": { "<0.3": 0, "0.3-0.6": 1, "0.6-0.8": 1, "0.8-1.0": 0 },
  "recent_feedback_7d": 1, "cross_validated": 0,
  "distilled_total": 0, "distilled_recent_7d": 0
}
```

Current `stats` also includes `retrieval` (`mode`: `vector` or `lexical`, plus `reason` when the vector channel is off), `attestation` (`attested`, plus a `warning` when `COMPOUND_MEMORY_AGENT_ID` is unset), and `expired_active` (active-area memories whose `valid_until` has passed; 0 in this snapshot). `cross_validated` counts memories whose `validated_by` lists at least two distinct agents — the single cross-host validation above correctly leaves it at 0. `memory_search` carries the same `retrieval` object. CLI `search` stays a bare array. CLI `stats` exits 1 when `COMPOUND_MEMORY_AGENT_ID` is unset or blank; `doctor` is the one-page check (attestation, retrieval, write lock, git) and uses the same rule, plus root / lock / git failures. MCP tools do not stop for a missing agent id.

Three things to notice:

- New memories start at `confidence` 0.5 and move on **evidence** — feedback carries an outcome: `success` raises it, `failure` lowers it (floor 0.05), `contradiction` freezes it into the review queue, `obsolete` archives immediately. Same-host `success` base bumps are recorded in `evidence.host_bumps` and cap at 0.3 for that host (a single host tops out at 0.8; ADR-0012).
- First validation from a different host earns an independent bonus (once per host per memory), with `validated_by` / `evidence` trails — confidence is evidence of correctness, not popularity.
- Hits embed one-hop neighbors automatically (empty here — no links yet; `memory_link` creates bidirectional links that get recalled for free).

## How compounding works

| Interest source | Mechanism |
|---|---|
| ① Usage reinforcement | Every `memory_feedback` advances `uses` and `last_used`. Confidence moves by outcome. Default `success`: +0.1, with that host's base bumps stored in `evidence.host_bumps` and capped at 0.3 (a single host tops out at 0.8; ADR-0012). `failure`: −0.2 (floor 0.05, repeats stack). `contradiction`: confidence frozen, a review-queue row is filed. `obsolete`: archived immediately. `unknown`: the event is recorded and confidence does not move |
| ② Link value | `memory_link` creates bidirectional links; `memory_get` pulls one-hop neighbors; `search` hits embed up to 3 compact neighbors (active memories only, `--no-neighbors` to disable) |
| ③ Distillation | `distill-plan` (CLI, deterministic candidates + `suggested_action`: merge / promote / review) → agent writes the product → `distill-apply` atomic commit (product links back to sources; sources archived but revivable) |
| ④ Cross-agent validation | On `success` only, the first feedback from a host other than `source` adds +0.15 (once per host per memory; recorded in `validated_by`; does not consume the same-host `host_bumps` cap) |

Scoring (weights are the `W_*` constants in `src/compound_memory/scoring.py`): `0.70·similarity + 0.15·confidence + 0.10·recency(0.5+0.5·e^(−Δt/τ)) + 0.05·type weight`. With the vector channel enabled, ranking switches to RRF fusion with an ε=0.04 prior tie-break (see the spec, "index as cache").

## The 5 MCP tools

| Tool | Purpose | Key points |
|---|---|---|
| `memory_write` | Write a memory | `type`: episode/fact/insight/skill/decision; `source`: your agent id; give fact/insight/decision a stable `key`; optional `valid_from`/`valid_until` (ISO dates), `project` scope, and `lesson_role` (`lesson` or `avoid`; omit for an ordinary memory). `content` over 262144 characters is rejected and nothing is written |
| `memory_search` | Retrieve | Returns `{"hits": [...], "count", "retrieval"}` ranked by score (default `top_k=5`); embeds up to 3 trimmed one-hop neighbors; dual-channel by default (`_shared` + caller's own private ns); optional `project` (fail-closed) and `explain`. CLI `search` stays a bare array |
| `memory_get` | Fetch by id | Always contains a `found` key; pulls one-hop neighbors with no count cap and keeps archived neighbors (search caps at 3, trims content to 80 characters, and drops archived neighbors). Expired neighbors are omitted on both paths. Private-ns targets require `reader` |
| `memory_link` | Link two memories | Bidirectional; both sides must be in the same ns; private-ns links require owner identity |
| `memory_feedback` | Report how a memory actually behaved | Every outcome advances `uses` and `last_used`. Default `outcome=success`: conf+0.1, same-host base capped at 0.3 via `host_bumps` (single host tops out at 0.8; ADR-0012); first cross-host validation +0.15 (exempt from the cap). `failure` −0.2 (floor 0.05); `contradiction` freezes confidence into the review queue; `obsolete` archives now; `unknown` records the event and leaves confidence unchanged. **Mandatory after adopting a hit** — that's the loop that makes the store compound |

Tool descriptions embed the protocol rules themselves, so agents keep the loop intact even without host-side rules injected. Short English loop: [`docs/agent-integration.en.md`](docs/agent-integration.en.md). Full parameter reference: `docs/agent-integration.md`.

## CLI

Checkout commands below (`uv run` inside a clone). Hosts that did not clone the repo use `uvx --from compound-memory compound-memory …` instead.

```bash
uv sync --extra dev              # first clone: build .venv (later `uv run` reuses it)

uv run compound-memory init               # initialize an empty store
uv run compound-memory write "Vercel Serverless has a 10s timeout" episode agent-workbuddy
uv run compound-memory search "Vercel timeout"   # default top_k=5; hits embed one-hop neighbors (limit 3, --no-neighbors to disable)
uv run compound-memory feedback <id> agent-claude
uv run compound-memory decay          # run from cron
uv run compound-memory revive <id>    # revive an archived memory
uv run compound-memory distill-plan   # distillation candidates: suggested_action is merge / promote / review; also report-only negative_salience and contradiction_pairs
uv run compound-memory dream --ns _shared   # read-only report: distill-plan section (including the negative sections) + decay_signals + equal stats snapshots (no writes, no LLM)
uv run compound-memory maintain            # one ops pass: read-only dream → optional decay → health-snapshot. Decay is off unless --with-decay or RUN_DECAY=1 (DECAY=1). No --propose, no dream-apply
uv run compound-memory dream --ns _shared --propose  # system proposes: qualifying clusters go to <root>/distill/dream-proposals.json; a human runs dream-apply. Default off; empty pack is []
uv run compound-memory dream-apply --proposals apply.json --source agent-workbuddy --dry-run  # gated apply preview; drop --dry-run to run distill-apply on admitted rows
uv run compound-memory distill-apply "the merged insight" insight agent-workbuddy --sources <id1>,<id2>  # atomic: product (links, origin=distillation) + source archival, one commit
uv run compound-memory doctor           # one-page health check; exit 1 if AGENT_ID is unset, root is missing, the lock is broken, or git is unusable
uv run compound-memory stats            # health: uses/confidence buckets + liveness + distillation yield; exit 1 if AGENT_ID is unset
uv run compound-memory rebuild-index  # rebuild the search cache anytime
uv run compound-memory review-queue   # conflict queue (CLI-only entry)
uv run compound-memory git-log        # audit trail
```

More operations: `explain <id>` (confidence composition + evidence detail for one memory), `forget <id> --agent <id>` (terminal removal, ADR-0009), `review-resolve` (adjudicate conflicts), `extract <transcript|dir>` (deterministic session-transcript mining).

## Architecture

```
Agent (MCP client / CLI)
  └─ memory_write | memory_search | memory_get | memory_link | memory_feedback
       └─ MemoryStore (~/.agents/memory)
            ├─ namespaces/_shared/{episode,fact,insight,skill,decision}/*.md   shared area
            ├─ namespaces/agent-*/...                                          private areas
            ├─ archive/...                                                     decayed archive (revivable)
            ├─ index/tokens.json                                               rebuildable lexical cache
            ├─ index/vectors.db                                                rebuildable vector cache (optional)
            ├─ review-queue.md                                                 fact/insight/decision conflict queue
            └─ .git/                                                           auto-commit on every write
```

## Scheduled distillation prep (launchd / cron / systemd)

Per ADR 0001, the deterministic prep runs on a schedule while judgment (summarizing / merging) stays with the calling agent. Every day at 09:00 `dream` writes a read-only report to `<root>/distill/last-plan.json` (the `distill` object is the candidate list; nothing is archived or forgotten). Schedulers keep calling `dream`; `dream-apply` is the explicit, caller-supplied apply and is not part of the timer. Pick one scheduler — **launchd** (macOS standard, catches up after sleep), **systemd user timer** (`Persistent=true`, same catch-up), or **cron** (most portable, no catch-up) — all three drive the same platform-neutral `scripts/distill-prepare.sh`. Ready-made templates: `scripts/com.compound-memory.distill-prepare.plist.tmpl` (launchd), `scripts/compound-memory-distill-prepare.{service,timer}.example` (systemd), and `scripts/compound-memory-distill-prepare.cron.example` (cron). The filled launchd / systemd / cron commands are in the Chinese README. The script runs `set -eu`: any failure exits non-zero (visible via `launchctl list` / `systemctl --user list-timers` / cron mail, log at `distill/prepare.log`). `distill/` is a runtime artifact directory (auto-gitignored) — no commit noise; only `distill-apply` after agent judgment lands one atomic commit.

## Daily maintenance (one timer)

`compound-memory maintain` is the single daily job: read-only `dream` (writes `<root>/distill/last-plan.json`, same report as `dream`, never `--propose`) → optional `decay` → `health-snapshot`. The snapshot runs after decay, so it records the post-decay store when decay ran. **Default is dream + health-snapshot. Decay stays off** unless you pass `--with-decay` or set `RUN_DECAY=1` (`DECAY=1` is an alias). Any failing step exits non-zero and names that step on stderr; later steps do not run.

`scripts/distill-prepare.sh` stays the dream-only prep path. Do not point a timer at `dream --propose`, `dream-apply`, or `distill-apply` (ADR-0001 / #72 / #97): proposals and merges stay an explicit human/agent step. `scripts/daily-maintain.sh` is a thin launchd/cron wrapper (resolves `uv` on `PATH`, then execs `compound-memory maintain`). It does not orchestrate the steps.

From a checkout, one cron line is enough (decay off):

```bash
0 9 * * * COMPOUND_MEMORY_ROOT=$HOME/.agents/memory $HOME/.local/bin/uv run --project <repo> compound-memory maintain >> $HOME/.agents/memory/distill/daily-maintain.log 2>&1
```

Same job with decay: prefix `RUN_DECAY=1`. A launchd `ProgramArguments` array can be the same `uv`, `run`, `--project`, `<repo>`, `compound-memory`, `maintain` (set `COMPOUND_MEMORY_ROOT` and, only if you want decay, `RUN_DECAY` under `EnvironmentVariables`). The thin wrapper is the PATH-proof alternative: `/bin/sh <repo>/scripts/daily-maintain.sh` with `UV_BIN` set. Keep using `distill-prepare.sh` when the timer should only refresh the dream report.

## Documentation

- [`CHANGELOG.md`](CHANGELOG.md) — release notes (0.5.x Windows support and later)
- [`docs/specs/0001-compound-memory-spec.md`](docs/specs/0001-compound-memory-spec.md) — design spec
- [`docs/agent-integration.en.md`](docs/agent-integration.en.md) — short English setup (uvx, required agent id, 5 tools, search → feedback → write)
- [`docs/agent-integration.md`](docs/agent-integration.md) — per-host MCP configs + the unified usage protocol (Chinese; full guide)
- [`docs/eval/README.md`](docs/eval/README.md) — how to record "can run" versus "did the same task change with memory". A checklist and a small fixed prompt set, not a measured effectiveness claim
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`CONTEXT.md`](CONTEXT.md) — glossary (Chinese)
- [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md) — usage rules for hosts (Chinese)

## Development

```bash
uv run pytest tests/ -q     # full suite (MCP tool boundary + distillation + lifecycle/index/CLI + input defense)
uv run mypy src/compound_memory/   # whole package, non-strict except the modules below
```

`storage.validation`, `storage.paths`, and `server` are checked with the per-module strict flags in `pyproject.toml` (the same `mypy` invocation CI runs on Ubuntu / Python 3.13). Other modules stay on the looser global config. Do not set `strict = true` inside a `[[tool.mypy.overrides]]` block: mypy applies that key to the global options and would strict-check the whole package.

Test seams: the MCP tool boundary via in-process `mcp.Client(server)` (no subprocess) plus unit tests for core modules (scoring / index / store ops). CI runs tests, type checks, and a pure-wheel install smoke on Linux and Windows across Python 3.11/3.12/3.13.

## Platform support

Python ≥ 3.11 on macOS, Linux, and Windows.

| | macOS / Linux | Windows |
|---|---|---|
| Install | `pip` / `uv` wheel or a clone | Same. CI installs the pure wheel and runs `init` / `write` / `search` / the server entry point |
| Write lock | `flock`, waits until the lock is free | `msvcrt` byte-range lock, polls up to 30s. If the wait expires the write **fails** (`WriteLockError`); it does not continue unlocked |
| Lock file cannot be created | Warning, write proceeds unlocked | Same. This is a broken filesystem, not lock contention |
| File permissions | POSIX mode bits preserved on atomic replace | No POSIX permissions; the permission-align step is skipped |
| Vector recall | Optional `vec` extra + a local BGE model | Same code path. Windows CI does not install the `vec` extra; missing it is lexical search, reported by `stats.retrieval` |
| Scheduled distill prep | launchd, systemd user timer, or cron (`distill-prepare.sh`, dream only) | No first-party Task Scheduler template; point cron or Task Scheduler at `scripts/distill-prepare.sh` yourself |
| Daily maintain | one cron/launchd job: `compound-memory maintain` (or `scripts/daily-maintain.sh`) | Same command. Decay only with `--with-decay` or `RUN_DECAY=1` |
| Git history | `git` on `PATH` | Same |

## Release

PyPI versions are immutable and the tag must match `pyproject.toml`'s `version` (the release workflow verifies this and fails loudly). Releases go through GitHub Actions + PyPI Trusted Publisher (OIDC, no token): push a tag like `v0.1.0` and `release.yml` builds and publishes automatically.

---

MCP Registry name: `mcp-name: io.github.chinwe/compound-memory`

`server.json` records the PyPI package `compound-memory` (stdio). It does not name a repo path. Host configs still use `uvx --from compound-memory compound-memory-server`, because the package name and the server console script differ.
