# SE3 — Software Engineering 3.0 Framework

![Version](https://img.shields.io/badge/version-10.7.1-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-Apache--2.0-lightgrey)

**English** | [中文](README.zh.md)

> **A project-level, cross-session flow framework where the program — not the human — supervises the AI agent. You prompt once, walk away, and come back to a finished deliverable.**

SE3 is not a single-session prompting tool, a skill, a subagent, or a dynamic workflow. Those are *in-session* aids that augment one human-in-the-loop turn. SE3 sits one layer above: it is a CLI engine + persistent state machine + code-first code↔spec governance that supervises an AI coding agent across many sessions, on many machines, until the work is actually done.

---

## Design Philosophy

### 1. A different paradigm: program-as-supervisor, human out-of-the-loop

Skills, subagents, and dynamic workflows make a *single AI turn* smarter or more parallel. They are valuable, but they assume a human is present, reading output and steering after every step.

SE3 makes a different bet. The unit of work is not a turn; it is a **project task**. Between `se3 run "…"` and the final commit there may be dozens of LLM calls across plan / implement / test / verify / commit steps, multiple agent rotations, fix loops, spec-guardrail rollbacks, and even multi-machine collaboration via the daemon and central server. The supervisor of all this is the SE3 engine — Python code running a deterministic state machine — not a person watching a terminal.

| Tool class | Scope | Who supervises | Where state lives |
|------------|-------|----------------|-------------------|
| Skills / subagents / dynamic workflows | One session, one turn (or a fan-out within one turn) | Human in the loop, reading output | Conversation context |
| **SE3** | A project task spanning many sessions / machines | The program (engine + daemon) | Persistent files (`se3/state/`, `se3/history/`, `se3/issues/`) |

### 2. The real pain: attention is all you need

LLMs are not the bottleneck. *Human attention* is. The cost of any agentic system is measured in how often it forces a person to read, judge, and decide. SE3's north star is **save human attention**.

The ideal SE3 session looks like this:

1. **Prompt** — you type `se3 run "…"` (or open a discovery session).
2. **Discover** — the engine asks a few targeted clarifying questions until requirements converge.
3. **Fire-and-forget** — you walk away. The engine plans, implements, tests, self-checks, verifies against the spec, updates the spec, bumps the version, and commits.
4. **Pick up the deliverable** — you come back to a clean commit on a branch, with the spec, version, and history already aligned.

Steps 1 and 2 are the only places where human attention is genuinely required. Everything else is the program's job.

### 3. The four moats that make this paradigm work

A program-as-supervisor paradigm only holds up if the framework provides four things that in-session tools cannot:

- **Cross-session state machine** — `se3/state/engine.json` persists the exact step, attempt, context, and fix-loop history of every flow. `se3 daemon` keeps a resident process supervising local `se3 run` flows; `se3-server` aggregates many daemons into one web view; `se3 run --loop` chains tasks autonomously on isolated git worktrees. The flow survives terminal exits, machine restarts, and hand-offs between machines. *Why this paradigm needs it:* without durable state, "walking away" loses the work.
- **Spec ↔ code two-way governance (asymmetric)** — `se3/specs/*/spec.md` is a documented snapshot of the code. The two directions are deliberately not equal. **code → spec is primary:** `se3 sync` regenerates the spec from the current code, and when the two disagree the code wins and the spec is updated — never the reverse. **spec → code is only a bounded, within-flow drift guard:** for the duration of a single flow, `se3 guardrails` treats the already-recorded SHALL/MUST requirements as the implementation contract *for that flow*, blocking silent weakening or deletion mid-flow; it does not make the spec authoritative over the code in general. *Why this paradigm needs it:* a long-running unattended agent will otherwise drift; the spec is the implementation contract for the duration of a flow.
- **Failure recovery built in** — `se3 salvage` rescues a crashed session by committing dangling changes, filing follow-up issues, and archiving the state. `se3/state/known_test_failures.json` distinguishes a new regression from a pre-existing red test. Issue discovery promotes any unresolved concern into a tracked `se3/issues/` record. *Why this paradigm needs it:* when no human is watching, the framework must catch its own failures rather than leak them.
- **Portable substrate** — the engine is pure Python over the file system. The LLM call layer is a thin `AgentRunner` adapter; today's concrete runner is the Claude Code CLI, but the abstraction (`AgentRunner` / `RunResult` / `InfraErrorType`) is provider-neutral. *Why this paradigm needs it:* a paradigm bet should not be a single-vendor bet.

### se3 vs Claude Code Dynamic Workflows (complementary, not competing)

Dynamic Workflows solve *in-session* parallelism: deterministic fan-out, judge panels, pipelines, all inside one orchestrating conversation. They make a single turn comprehensive and confident.

SE3 solves *cross-session* project governance: persistent state, code↔spec governance, failure recovery, and a portable substrate that outlives any single conversation.

The two compose. A future SE3 step can delegate its in-step parallel work to a Dynamic Workflow without changing SE3's outer state machine. We deliberately do not pin to specific DW API names here, because DW is still in research preview and its surface will evolve.

---

## Installation

```bash
# Core CLI (Python 3.8+)
pip install se3

# With the central server / web console
pip install 'se3[server]'

# With the headless-browser acceptance test (needs `playwright install chromium` afterwards)
pip install 'se3[browser]'
```

Current version: **8.0.0**. Two console scripts are installed:

| Script | Purpose |
|--------|---------|
| `se3` | Core CLI (always available) |
| `se3-server` | Central web server (only with the `server` extra) |

The core CLI never imports the web stack, so installing without `[server]` keeps the dependency surface minimal.

---

## Quick Start

```bash
# 1. Initialize a project (creates se3.yaml, se3/specs/base/spec.md, .gitignore, git repo)
cd your-project
se3 init

# 2. Optional: explore vague requirements through multi-turn discovery first
se3 run --discover "I want a CLI tool that does X"

# 3. Run a task end-to-end (analyze → plan → implement → test → self-check →
#    verify_spec → update_spec → version_analyze → commit)
se3 run "Add JWT authentication"

# 4. Resume an interrupted flow exactly where it stopped
se3 run --resume
```

### Three operating modes

- **`--loop`** — Run tasks back-to-back on an isolated git worktree branch
  (`loop/<slug>-<n>`). Each iteration gets its own clean working tree; the
  branch is auto-merged or auto-discarded when the loop ends, or preserved
  for deferred merge if you Ctrl-C.
- **`se3 daemon start`** — Launch a resident background process that
  supervises every local `se3 run`, aggregates state under
  `se3/state|logs|calls|issues`, and (optionally) dials out to a central
  server. Lets you check on a flow from anywhere.
- **`se3-server`** — A FastAPI + WebSocket central server (with a bundled
  static web console at `/`) that merges many daemons into one multi-machine
  view. Useful for fleets, remote launch, and watching long-running flows
  from a browser. Defaults to `127.0.0.1:8080`.

#### Web console authentication (since 8.0.0)

The central server is a multi-tenant control plane — the web console and REST
API require a login, and every machine / flow is scoped to the owner that owns
it. The first-run flow is:

1. **Mint a break-glass admin token** — run `se3-server bootstrap-token` once;
   it prints a one-time admin token to the console.
2. **Log in** — open the web console and exchange the token for the break-glass
   admin session (`POST /api/auth/breakglass`).
3. **Create local users** — as admin, invite/create accounts (`POST /api/users`).
   v1 has no public self-service registration.
4. **Issue a daemon key** — each owner self-mints a daemon key in the UI
   (`POST /api/daemon-keys`), then binds a worker with
   `se3 daemon start --daemon-key <key>`. The owner only ever sees their own
   machines and flows.

See [docs/daemon-and-server.md](docs/daemon-and-server.md#authentication--multi-tenant-access)
for the full end-to-end auth walkthrough and configuration keys.

---

## Command Reference

All commands found below are present in `src/se3/cli.py` or its registered
sub-typers as of version 8.0.0.

### Top-level commands

| Command | Purpose |
|---------|---------|
| `se3 run [TASK]` | Unified entry point. Drives the flow engine state machine (analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit). Supports `--resume`, `--flow-id`, `--loop`, `--max-iterations`, `--no-worktree`, `--merge`, `--list-loops`, `--discover`, `--from-issue`, `--change`, `--type`, `--preset`, `--output-format`. |
| `se3 init` | Initialize a new project: writes `se3.yaml`, base spec, `.gitignore`, and runs `git init` if needed. Flags: `--project-root`, `--name`, `--force`. |
| `se3 guardrails <spec-file>` | Run SE3 spec guardrails on a spec file (deleted-requirement / weakened-language detection). Used by CI and by `se3 merge`. Flag: `--original` / `-o <original-file>` to compare against a specific baseline. |
| `se3 sync` | One-directional code → spec sync. Iterates rounds until convergence. Flags include `--once`, `--max-rounds`, `--stable-rounds`, `--interactive`, `--show-diff`, `--validate-only`, `--resume`, `--force`, `--confirm-cleanup`. |
| `se3 sync-respond <call-file>` | Apply a human decision file produced by `se3 sync --interactive` for high-impact requirement deletions. |
| `se3 merge <branch> [<branch> ...]` | Sequentially merge branches into HEAD with LLM-driven conflict resolution. Flags: `--strategy fast\|safe\|strict`, `--delete-merged` / `--no-delete-merged`. Runtime data under `se3/` is synchronized per the tiered policy. |
| `se3 merge-respond <call-file>` | Apply a human decision file produced by `se3 merge` when conflicts or guardrail violations escalated to a human MCP call. |
| `se3 salvage` | Best-effort recovery of an abnormally terminated session: tolerant state load, commit dangling diff, file follow-up issues, archive the session. Flag: `--project-root` / `-p <path>`. |

### `se3 history` — flow history

| Subcommand | Purpose |
|------------|---------|
| `se3 history` / `se3 history list` | List flows across active state, archived state, and history-only directories. Flags: `--active-only`, `--archived-only`, `--json`. |
| `se3 history show <flow_id>` | Show structured step-by-step details. Flags: `--detailed` (LLM call breakdown), `--verbose` (full tool-call stream), `--json`. |
| `se3 history restore <flow_id>` | Resume a specific flow by ID (delegates to `se3 run --resume --flow-id`). `--dry-run` prints the command without executing. |
| `se3 history archived` | List only archived flows. `--json` for machine-readable output. |

### `se3 issue` — project issues

| Subcommand | Purpose |
|------------|---------|
| `se3 issue` / `se3 issue list` | List open issues (default). `--all` includes closed; `--type <t>` filters by type. |
| `se3 issue show <id>` | Render an issue's full details. |
| `se3 issue create` | Interactively create a new issue (title, description, type, priority, tags). |
| `se3 issue reset <id>` | Reset an in-progress issue back to `open`. |

### `se3 daemon` — resident control plane

| Subcommand | Purpose |
|------------|---------|
| `se3 daemon start` | Start the daemon. `--foreground` keeps it attached; `--server-url <ws://…>` registers with a central server; `--daemon-key <key>` binds this machine to an owner on a multi-tenant server. |
| `se3 daemon stop` | Stop the running daemon. |
| `se3 daemon status` | Report run state, machine id, server URL, real connection state, and tracked flows. `--json` for machine-readable output. |

---

## Directory Layout

Everything under `se3/` is gitignored by default *except* the whitelisted
sub-paths shown below (specs, issues, scripts, prompts, and `version-rules.md`
are tracked; runtime state and logs are not).

```
your-project/
├── se3.yaml                       # Project config (tracked)
├── se3.local.yaml                 # Local override   (gitignored)
├── pyproject.toml                 # Single source of truth for project version
├── VERSIONS.md                    # Changelog (maintained by documentation-updater)
├── scripts/                       # Helper scripts
├── .gitignore                     # Written / extended by `se3 init`
└── se3/                           # SE3 runtime root
    ├── specs/                     # ✅ tracked — documented snapshot of code
    │   ├── base/spec.md           # Base project spec, auto-loaded in every flow
    │   └── <capability>/spec.md
    ├── issues/                    # ✅ tracked — open/ and closed/ YAML records
    ├── prompts/                   # ✅ tracked — project-level preset prompt bodies (se3 run --preset)
    ├── version-rules.md           # ✅ tracked — optional, not present by default
    ├── state/                     # ❌ runtime — engine.json, sync_state.json, …
    │   └── archive/               #   archived engine snapshots
    ├── history/                   # ❌ runtime — per-flow per-step jsonl conversations
    ├── logs/                      # ❌ runtime — execution logs (incl. logs/llm/ traces)
    ├── calls/                     # ❌ runtime — pending human MCP call files
    ├── collab/                    # ❌ runtime — multi-agent collaboration state
    ├── cache/                     # ❌ runtime — derived caches (e.g. spec-index)
    ├── tmp/                       # ❌ runtime — transient prompt/response snapshots
    └── worktrees/                 # ❌ runtime — loop-mode / DAG isolation worktrees
```

---

## Specs Catalog

SE3 ships 24 self-describing specs under `se3/specs/`. They are the project's
living documentation — a code-first snapshot of what the code currently does —
which `se3 guardrails` protects from silent weakening within a flow. Use this
as your index into the codebase.

| Spec | One-line purpose |
|------|------------------|
| `base` | Project identity, directory layout, coding & workflow conventions; auto-loaded in every flow. |
| `se3-commands` | CLI surface contract for all top-level `se3 *` commands and their options. |
| `se3-config` | `se3.yaml` / `se3.local.yaml` schema and load/override semantics. |
| `se3-scaffold` | Standard project structure and what `se3 init` creates. |
| `se3-workflows` | The five workflow types (feature / bugfix / review / small / directive) + discovery, and which steps each runs. |
| `se3-versioning` | SemVer 2.0.0 rules, single-source version file, automatic bump contract. |
| `session-protocol` | Session startup, resume, loop mode lifecycle, branch isolation, and merge-back rules. |
| `flow-engine` | The core state machine — step pool, transitions, event stream, sinks, prompt markers, fix loops. |
| `agent-runner-infrastructure` | `AgentRunner` ABC and the `ClaudeCodeRunner` adapter: subprocess, hang detection, oversized-prompt rerouting. |
| `llm-caller` | Agent rotation, retry-context injection, JSON extraction modes, NDJSON streaming. |
| `dag-scheduler` | Parallel DAG executor for the implement step (relay worktrees, transitive reduction). |
| `worktree-management` | Loop / merge worktree lifecycle, branch naming, cleanup of orphaned worktrees. |
| `requirement-intake` | How new requirements enter SE3 through `se3 run` (intake contract). |
| `preset-prompts` | Built-in + project two-layer preset prompt registry reused by `se3 run --preset` for standardized recurring tasks. |
| `spec-format` | Spec-format v1 grammar: marker, headings, `### Requirement:` items, scenarios. |
| `spec-guardrails` | Rules that block silent weakening / deletion of existing requirements. |
| `spec-role` | The spec's role as a documented snapshot of code (spec-assistant): code → spec is primary, with no routine manual-edit entry. |
| `issue-management` | `se3 issue` CLI and `IssueManager` storage API (YAML on disk, state machine). |
| `issue-discovery` | Automatic discovery of issues from flow execution and unresolved concerns. |
| `documentation-updater` | `README.md` badge updates and `VERSIONS.md` changelog generation. |
| `salvage-command` | Five-step best-effort recovery pipeline for crashed sessions. |
| `user-interjection-handling` | Ctrl-C interjection lifecycle and call-file routing across CLI / daemon / web. |
| `running-flow-console` | Web console behavior for the live, full-screen running-flow view. |
| `test-project` | The end-to-end test project used to exercise `se3 run` workflows. |

---

## Version & License

- Version is owned by `pyproject.toml` (`8.0.0`) and bumped by the engine's `version_analyze` + `commit` steps. Do not hand-edit it.
- License: Apache-2.0.
- See [VERSIONS.md](VERSIONS.md) for the full changelog.
