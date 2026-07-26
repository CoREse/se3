# tianluo (田螺) — the Software Engineering 3.0 flow engine

![Version](https://img.shields.io/badge/version-12.0.0-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

**English** | [中文](README.zh.md)

> **A project-level, cross-session flow framework where the program — not the human — supervises the AI agent. You prompt once, walk away, and come back to a finished deliverable.**

*tianluo is named after the Snail Girl (田螺姑娘) of Chinese folklore: a farmer comes home each day to find the housework quietly finished — the spirit did the work while he was away, never asking, never interrupting. That is this tool's contract. You call it by its short name: the command is `luo`.*

tianluo (formerly published as *se3*; the methodology is still called **SE 3.0**) is not a single-session prompting tool, a skill, a subagent, or a dynamic workflow. Those are *in-session* aids that augment one human-in-the-loop turn. tianluo sits one layer above: it is a CLI engine + persistent state machine + a code-first knowledge system (code-index + charter + why-comments) that supervises an AI coding agent across many sessions, on many machines, until the work is actually done.

---

## Design Philosophy

### 1. A different paradigm: program-as-supervisor, human out-of-the-loop

Skills, subagents, and dynamic workflows make a *single AI turn* smarter or more parallel. They are valuable, but they assume a human is present, reading output and steering after every step.

tianluo makes a different bet. The unit of work is not a turn; it is a **project task**. Between `luo run "…"` and the final commit there may be dozens of LLM calls across plan / implement / test / self-check / invariant-check / commit steps, multiple agent rotations, fix loops, and even multi-machine collaboration via the daemon and central server. The supervisor of all this is the tianluo engine — Python code running a deterministic state machine — not a person watching a terminal.

| Tool class | Scope | Who supervises | Where state lives |
|------------|-------|----------------|-------------------|
| Skills / subagents / dynamic workflows | One session, one turn (or a fan-out within one turn) | Human in the loop, reading output | Conversation context |
| **tianluo** | A project task spanning many sessions / machines | The program (engine + daemon) | Persistent files (`tianluo/state/`, `tianluo/history/`, `tianluo/issues/`) |

### 2. The real pain: attention is all you need

LLMs are not the bottleneck. *Human attention* is. The cost of any agentic system is measured in how often it forces a person to read, judge, and decide. tianluo's north star is **save human attention**.

The ideal tianluo session looks like this:

1. **Prompt** — you type `luo run "…"` (or open a discovery session).
2. **Discover** — the engine asks a few targeted clarifying questions until requirements converge.
3. **Fire-and-forget** — you walk away. The engine plans, implements, tests, self-checks, checks the diff against recorded invariants, flags any charter drift, bumps the version, and commits.
4. **Pick up the deliverable** — you come back to a clean commit on a branch, with the version, history, and code-index already aligned.

Steps 1 and 2 are the only places where human attention is genuinely required. Everything else is the program's job.

### 3. The four moats that make this paradigm work

A program-as-supervisor paradigm only holds up if the framework provides four things that in-session tools cannot:

- **Cross-session state machine** — `tianluo/state/engine.json` persists the exact step, attempt, context, and fix-loop history of every flow. `luo daemon` keeps a resident process supervising local `luo run` flows; `tianluo-server` aggregates many daemons into one web view; `luo run --loop` chains tasks autonomously on isolated git worktrees. The flow survives terminal exits, machine restarts, and hand-offs between machines. *Why this paradigm needs it:* without durable state, "walking away" loses the work.
- **A code-first knowledge system (code-index + charter + why-comments)** — the source of truth is the code itself. A `tianluo/code-index.md` structure map (auto-maintained, self-freshening) gives the agent an orientation map of *what modules and symbols exist and where*; a small hand-maintained `tianluo/charter.md` carries only the high-altitude facts every step needs in full (project identity, top-level architecture, project-wide invariants); colocated why-comments carry intent the code cannot express. *Why this paradigm needs it:* a long-running unattended agent needs to orient itself in the codebase cheaply on every step without a curated mirror of the code rotting beside it. See [The knowledge system](#the-knowledge-system-code-index--charter--why-comments) below for why this beats the spec-mirror it replaces.
- **Failure recovery built in** — `luo salvage` rescues a crashed session by committing dangling changes, filing follow-up issues, and archiving the state. The test-baseline cache distinguishes a new regression from a pre-existing red test. Issue discovery promotes any unresolved concern into a tracked `tianluo/issues/` record. *Why this paradigm needs it:* when no human is watching, the framework must catch its own failures rather than leak them.
- **Portable substrate** — the engine is pure Python over the file system. The LLM call layer is a thin `AgentRunner` adapter; today's concrete runner is the Claude Code CLI, but the abstraction (`AgentRunner` / `RunResult` / `InfraErrorType`) is provider-neutral. *Why this paradigm needs it:* a paradigm bet should not be a single-vendor bet.

### luo vs Claude Code Dynamic Workflows (complementary, not competing)

Dynamic Workflows solve *in-session* parallelism: deterministic fan-out, judge panels, pipelines, all inside one orchestrating conversation. They make a single turn comprehensive and confident.

tianluo solves *cross-session* project governance: persistent state, a code-first knowledge system, failure recovery, and a portable substrate that outlives any single conversation.

The two compose. A future tianluo step can delegate its in-step parallel work to a Dynamic Workflow without changing tianluo's outer state machine. We deliberately do not pin to specific DW API names here, because DW is still in research preview and its surface will evolve.

---

## The knowledge system: code-index + charter + why-comments

Earlier tianluo versions kept a parallel corpus of `tianluo/specs/**/spec.md` files — a curated prose mirror of the code — plus an entire governance machine to keep it from drifting: `luo sync` rounds, per-requirement drift baselines, `verify_spec` / `update_spec` / `spec_gate` flow steps, and the whole `sync_*` analyzer/loop/state/discovery stack. tianluo replaced that mirror with three colocated artifacts whose source of truth is the code itself.

### The three pieces

- **code-index** — a *structure map* of the project. Its structure comes deterministically from the code (a filesystem walk + Python AST symbol enumeration: directory/package → file/module → class → function/method); a one-line LLM summary, synthesized bottom-up (a directory's summary from its files', a file's from its symbols'), is attached to each level. It lands as **one self-sufficient file**, `tianluo/code-index.md` — the **authoritative product, committed to git**. It *is* the map, and it is what `luo code-index` renders and what gets injected into every flow step. Because it is plain text in a diff, a wrong summary can be spotted by a human reviewer and corrected, and the correction lands durably. Each node line also carries an embedded content fingerprint (a terse, render-invisible HTML comment), so the committed md *alone* decides what changed: on rebuild only fingerprint-changed nodes are re-summarized by the LLM, unchanged nodes reuse their existing summary (so human corrections survive), and the md is flushed periodically during a build so a crash resumes from where it stopped. There is no separate cache file — structure, summaries, and fingerprints all live in the one committed, human-diffable file.

  The structure comes from the **code**, not the json; the json is just a rebuild accelerator. Display reads only the `.md`. The optimization goal is **structural coverage, not summary depth** — the map answers *which modules/symbols exist and where*, and deliberately does not descend into implementation detail (that is the source code's job; copying it into the index would just reproduce a worse-than-code mirror).

- **charter** — `tianluo/charter.md`, the slimmed, renamed successor of the old base spec. It is injected, in full, into every step, and doubles as the conventions channel for sandboxed sub-processes (which cannot read `CLAUDE.md`). An *altitude gate* admits only what is **un-sayable in code and needed in full by the whole project**: project identity, top-level architecture, and project-wide cross-cutting invariants. The per-module locator index that used to bloat the base spec is gone — that job belongs to code-index. A byte threshold is a monitoring light, not a hard wall: because charter content is decoupled from project size (it grows with architectural complexity, not LOC), full-loading it stays cheap even on large projects; if it ever grows hard to load in full, that is a red flag that low-altitude content leaked in — not a reason to build an index over the charter.

- **why-comments** — colocated comments that carry *only* the why/intent that code cannot express, updated only when the why changes. They are not a source for code-index, so there is no per-change synchronization tax; the implement step's prompt simply asks the agent to update the colocated why-comment when a change's intent changes. This is honestly a prompt-level soft convention (same strength as the other conventions), pressing the comment-discipline surface to its minimum rather than eliminating it.

### What actually got better (an honest accounting)

This refactor does **not** make code descriptions more semantically correct: an LLM-generated summary can be wrong in exactly the same way a hand-written spec was. The real gains are elsewhere:

- **Source of truth returns to the code.** Navigation and intent live next to the code, not in a separate corpus that has to be kept honest.
- **Staleness is eliminated.** code-index regenerates incrementally with zero discipline required: a deterministic enumerator re-walks the tree every build, so a newly added symbol is enumerated, a deleted one is pruned, and only fingerprint-changed symbols are re-summarized. Completeness is a *property of the enumerator*, not of LLM diligence — the LLM only summarizes the symbols it is handed and never decides who is included, so it cannot omit a symbol, and a mis-summarized line still appears on the map.
- **The governance maintenance surface collapses.** The entire `sync_*` stack, `verify_spec`, `update_spec`, `spec_gate`, the per-requirement drift baselines, and the old `spec_check` all retire. What remains is two cheap, anchored checks: `INVARIANT_CHECK` (does the diff violate any *already-recorded* binding invariant — anchored to {task description, charter, the touched code's why-comments}?) and `CHARTER_FRESHNESS` (an advisory that flags only when the diff plausibly touches one of charter's content classes, and otherwise passes for free).
- **Granularity and admission become explicit knobs.** code-index granularity bottoms out at each file's smallest *natural* semantic unit (code → function/method; structured non-code → its natural unit; opaque files → one file-level line), with line/byte chunking only as a last-resort degrade mode gated behind three simultaneous conditions; the four thresholds are exposed via `luo config`. Charter content is gated by an admission standard you can read and enforce. Both are dials you turn, not emergent behavior you fight.
- **Charter volume is decoupled from project scale.** It grows with architecture, not lines of code.
- **The failure floor is higher than the old system's.** Even if every soft discipline lapses, the one automatically-maintained artifact — code-index — stays self-fresh. The system's worst case is therefore strictly better than the old system's worst case of *a rotting spec corpus + grep*.

### A concrete before/after — and why spec-index could never win this

Take the old `spec_index.py` (~1130 lines — itself retired by this very refactor) as a worked example. Suppose you need to answer a *navigation* question about it: where is it, what does it do, what are its key symbols?

Without a code-index, you have to read the whole ~1130-line file into context to answer even that. With a code-index, you first read the few map lines about that file — for instance, *"builds an item-level spec index, incremental invalidation via mtime + size + sha256; key symbols `load_or_build` / `_make_summary` / `_extract_locator` / `_h4_dividers`."* Navigation questions never touch the source. And a *precise* question — say, the exact boundary condition of one heuristic — needs only a pinpoint read of those ~30 lines, not the whole file.

**The comparison with spec-index is the sharpest point.** A spec / spec-index has an upside that is *fundamentally capped by living one layer above the code*: even assuming a spec were perfectly accurate and perfectly complete, it still sits at spec altitude and cannot surface the actual code-level detail — so after it locates the file for you, you still have to go back and read the code, and to be thorough you have to read all of it. The spec's likely inaccuracy and incompleteness is merely insult on top of that injury; it is **not** the reason it loses to code-index. code-index is not subject to this cap at the root, because its source of truth *is* the code and it walks you straight to those ~30 lines.

This is exactly the **coverage > depth** bet cashing out: the map's job is to tell you which ~30 lines to flip to — not to replace those ~30 lines. And that context saving is not a one-time win: it compounds on **every step of every flow**, which is precisely the cost code-index exists to cut.

> Historical decisions and retained-but-removed intent (e.g. a feature pulled out while its intent is kept on record) do not enter the charter; they continue through the issue channel (`luo issue`). Cross-file architectural decisions with no single owner enter the charter, hand-maintained, accepting that they cannot be auto-synced.

---

## Installation

```bash
# Core CLI (Python 3.8+)
pip install tianluo

# With the central server / web console
pip install 'tianluo[server]'

# With the headless-browser acceptance test (needs `playwright install chromium` afterwards)
pip install 'tianluo[browser]'
```

Current version: **12.0.0**. The installed console scripts:

| Script | Purpose |
|--------|---------|
| `luo` | **The** command. Short for tianluo — you write `tianluo` down, you call `luo` to work. |
| `tianluo` | Full-name entry, identical to `luo` (docs, demos, discoverability) |
| `se3` | Transitional alias from the rename; prints a migration notice, removed in 13.0.0 |
| `tianluo-server` | Central web server (only with the `server` extra) |
| `se3-server` | Transitional alias for `tianluo-server`, removed in 13.0.0 |

The core CLI never imports the web stack, so installing without `[server]` keeps the dependency surface minimal.

> Prefer a two-letter command? `alias tl=luo` — we deliberately don't ship `tl`
> (the Teal compiler owns it, and two competing short commands would split the
> docs and community vocabulary).

### Migrating an existing se3 project

Everything keeps working unchanged through 12.x: the `se3` command, a legacy
`se3/` runtime directory, and `se3.yaml` / `se3.local.yaml` configs are all
still honoured. To move a project to the new layout in one reviewable,
`git revert`-able commit:

```bash
luo migrate run rename-to-tianluo   # git mv se3/ → tianluo/, config renames, .gitignore rewrite
```

All legacy fallbacks are removed in **13.0.0**.

---

## Quick Start

```bash
# 1. Initialize a project (creates tianluo.yaml, tianluo/charter.md, .gitignore, git repo)
cd your-project
luo init

# 2. Optional: explore vague requirements through multi-turn discovery first
luo run --discover "I want a CLI tool that does X"

# 3. Run a task end-to-end (analyze → plan → implement → test → self_check →
#    invariant_check → charter_freshness → version_analyze → commit → summarize)
luo run "Add JWT authentication"

# 4. Resume an interrupted flow exactly where it stopped
luo run --resume

# 5. Navigate the codebase via the structure map
luo code-index                          # adaptive root map: a budgeted, zoomable directory tree
luo code-index index src/tianluo/engine     # drill one literal level (a directory's immediate children)
luo code-index show src/tianluo/cli.py      # one file's full function/method detail
```

### Three operating modes

- **`--loop`** — Run tasks back-to-back on an isolated git worktree branch
  (`loop/<slug>-<n>`). Each iteration gets its own clean working tree; the
  branch is auto-merged or auto-discarded when the loop ends, or preserved
  for deferred merge if you Ctrl-C.
- **`luo daemon start`** — Launch a resident background process that
  supervises every local `luo run`, aggregates state under
  `tianluo/state|logs|calls|issues`, and (optionally) dials out to a central
  server. Lets you check on a flow from anywhere.
- **`tianluo-server`** — A FastAPI + WebSocket central server (with a bundled
  static web console at `/`) that merges many daemons into one multi-machine
  view. Useful for fleets, remote launch, and watching long-running flows
  from a browser. Defaults to `127.0.0.1:8080`.

#### Web console authentication

The central server is a multi-tenant control plane — the web console and REST
API require a login, and every machine / flow is scoped to the owner that owns
it. The first-run flow is:

1. **Mint a break-glass admin token** — run `tianluo-server bootstrap-token` once;
   it prints a one-time admin token to the console.
2. **Log in** — open the web console and exchange the token for the break-glass
   admin session (`POST /api/auth/breakglass`).
3. **Create local users** — as admin, invite/create accounts (`POST /api/users`).
   v1 has no public self-service registration.
4. **Issue a daemon key** — each owner self-mints a daemon key in the UI
   (`POST /api/daemon-keys`), then binds a worker with
   `luo daemon start --daemon-key <key>`. The owner only ever sees their own
   machines and flows.

See [docs/daemon-and-server.md](docs/daemon-and-server.md#authentication--multi-tenant-access)
for the full end-to-end auth walkthrough and configuration keys.

---

## Command Reference

All commands found below are present in `src/tianluo/cli.py` or its registered
sub-typers as of version 12.0.0.

### Top-level commands

| Command | Purpose |
|---------|---------|
| `luo run [TASK]` | Unified entry point. Drives the flow engine state machine (analyze → plan → implement → test → self_check → invariant_check → charter_freshness → version_analyze → commit → summarize). Supports `--resume`, `--flow-id`, `--loop`, `--max-iterations`, `--no-worktree`, `--merge`, `--list-loops`, `--discover`, `--from-issue`, `--change`, `--type`, `--preset`, `--output-format`. |
| `luo init` | Initialize a new project: writes `tianluo.yaml`, `tianluo/charter.md`, `.gitignore`, and runs `git init` if needed. Flags: `--project-root`, `--name`, `--force`. |
| `luo code-index` | Render the **adaptive root map** from `tianluo/code-index.md`: a byte-budgeted, zoomable directory tree (top level always shown; code directories expanded a few levels deep within the budget). This is the same map injected into every flow step. Reads the committed map (reports "not built" until you run `rebuild`); flow steps keep it fresh lazily/incrementally. |
| `luo code-index index [PATH]` | Render exactly **one literal level** at `PATH`: a directory's immediate children (subdirs + files), or a file's functions/methods. No argument → the literal root level. Unlike the bare command, it never auto-expands. |
| `luo code-index show <path>` | Print one file's full function/method detail (and any degraded chunks) from the structure map. |
| `luo code-index rebuild [--force]` | Rebuild the code-index, flushing the md periodically as a checkpoint. Incremental by default (only fingerprint-changed nodes are re-summarized); `--force` re-summarizes everything. |
| `luo code-index inspect` | Show code-index stats (file / symbol / degraded-chunk counts) from the on-disk map. |
| `luo migrate run <id>` / `luo migrate list` | Run a registered version/format migration (`run <id>`), or list the available migrators (`list`). A reusable registry skeleton; the first migrator (`spec-to-new-system`) converts a legacy `tianluo/specs/` project to the code-index + charter + why-comments system in one reviewable, `git revert`-able change. |
| `luo guardrails <spec-file>` | Run tianluo guardrails on a file (deleted-line / weakened-language detection); `--sizes` runs project-wide size checks. Used by `luo merge`. Flag: `--original` / `-o <baseline-file>`. |
| `luo merge <branch> [<branch> ...]` | Sequentially merge branches into HEAD with LLM-driven conflict resolution. Flags: `--strategy fast\|safe\|strict`, `--delete-merged` / `--no-delete-merged`. Runtime data under `tianluo/` is synchronized per the tiered policy. |
| `luo merge-respond <call-file>` | Apply a human decision file produced by `luo merge` when conflicts or guardrail violations escalated to a human MCP call. |
| `luo salvage` | Best-effort recovery of an abnormally terminated session: tolerant state load, commit dangling diff, file follow-up issues, archive the session. Flag: `--project-root` / `-p <path>`. |

### `luo history` — flow history

| Subcommand | Purpose |
|------------|---------|
| `luo history` / `luo history list` | List flows across active state, archived state, and history-only directories. Flags: `--active-only`, `--archived-only`, `--json`. |
| `luo history show <flow_id>` | Show structured step-by-step details. Flags: `--detailed` (LLM call breakdown), `--verbose` (full tool-call stream), `--json`. |
| `luo history restore <flow_id>` | Resume a specific flow by ID (delegates to `luo run --resume --flow-id`). `--dry-run` prints the command without executing. |
| `luo history archived` | List only archived flows. `--json` for machine-readable output. |

### `luo issue` — project issues

| Subcommand | Purpose |
|------------|---------|
| `luo issue` / `luo issue list` | List open issues (default). `--all` includes closed; `--type <t>` filters by type. |
| `luo issue show <id>` | Render an issue's full details. |
| `luo issue create` | Interactively create a new issue (title, description, type, priority, tags). |
| `luo issue reset <id>` | Reset an in-progress issue back to `open`. |

### `luo daemon` — resident control plane

| Subcommand | Purpose |
|------------|---------|
| `luo daemon start` | Start the daemon. `--foreground` keeps it attached; `--server-url <ws://…>` registers with a central server; `--daemon-key <key>` binds this machine to an owner on a multi-tenant server. |
| `luo daemon stop` | Stop the running daemon. |
| `luo daemon status` | Report run state, machine id, server URL, real connection state, and tracked flows. `--json` for machine-readable output. |

---

## Directory Layout

Everything under `tianluo/` is gitignored by default *except* the whitelisted
sub-paths shown below (the code-index map, charter, issues, scripts, prompts,
and `version-rules.md` are tracked; runtime state and logs are not).

```
your-project/
├── tianluo.yaml                       # Project config (tracked)
├── tianluo.local.yaml                 # Local override   (gitignored)
├── pyproject.toml                 # Single source of truth for project version
├── VERSIONS.md                    # Changelog (maintained by documentation-updater)
├── scripts/                       # Helper scripts
├── .gitignore                     # Written / extended by `luo init`
└── tianluo/                           # tianluo runtime root
    ├── code-index.md             # ✅ tracked — authoritative structure map (LLM-injected, human-reviewable)
    ├── charter.md                # ✅ tracked — project identity / architecture / invariants, injected in full every step
    ├── issues/                   # ✅ tracked — open/ and closed/ YAML records
    ├── prompts/                  # ✅ tracked — project-level preset prompt bodies (luo run --preset)
    ├── version-rules.md          # ✅ tracked — optional, not present by default
    ├── state/                    # ❌ runtime — engine.json, …
    │   └── archive/              #   archived engine snapshots
    ├── history/                  # ❌ runtime — per-flow per-step jsonl conversations
    ├── logs/                     # ❌ runtime — execution logs (incl. logs/llm/ traces)
    ├── calls/                    # ❌ runtime — pending human MCP call files
    ├── cache/                    # ❌ runtime — derived caches (build locks, etc.)
    ├── tmp/                      # ❌ runtime — transient prompt/response snapshots
    └── worktrees/                # ❌ runtime — loop-mode / DAG isolation worktrees
```

---

## Navigating the codebase

The code-index *is* the index into this codebase. Start at the root view and
drill down — you read the map's few lines first, and open source files only
when you need the implementation detail behind a specific symbol:

```bash
luo code-index                           # the adaptive root map (budgeted zoomable tree)
luo code-index index src/tianluo/engine      # one level: the engine package's immediate children
luo code-index show src/tianluo/engine/code_index.py   # that file's full symbol tree
```

The same root-view map is injected automatically into every flow step, so the
agent always carries a project-wide orientation map; deeper function-level
detail is fetched on demand. Charter (`tianluo/charter.md`) is injected in full
alongside it and carries the high-altitude facts — project identity, top-level
architecture, and project-wide invariants — that every step needs to see whole.

---

## Version & License

- Version is owned by `pyproject.toml` (`12.0.0`) and bumped by the engine's `version_analyze` + `commit` steps. Do not hand-edit it.
- License: Apache-2.0.
- See [VERSIONS.md](VERSIONS.md) for the full changelog.
