# SE3 — Software Engineering 3.0 Framework

![Version](https://img.shields.io/badge/version-3.38.1-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

**A spec-driven flow engine that turns AI coding assistants into disciplined software engineers.**

[中文版 README](README.zh.md)

---

## Motivation

AI coding assistants are powerful but chaotic. Give one a complex task and it will:

- **Lose context** across sessions — no memory of what was done, what's left, or why decisions were made
- **Skip engineering discipline** — jump straight to code without analysis, design, or spec review
- **Drift from specifications** — silently weaken or ignore existing requirements
- **Produce unverified work** — commit without testing, or mark things "done" without checks
- **Lack workflow structure** — no concept of feature vs. bugfix vs. review, no adaptive process

SE3 solves this by wrapping AI agents in a **program-driven state machine**: an 11-step flow engine that enforces analysis → design → implementation → testing → verification → commit, adapting the process to the type and scope of each task. The AI still does the thinking; SE3 ensures it thinks in the right order.

## Highlights

- **Unified `se3 run` entry point** — one command for all workflows: features, bugfixes, reviews, small changes, directives
- **11-step flow engine** — a state machine that orchestrates analyze → propose → design → plan → implement → test → verify → commit, with automatic step selection per workflow type
- **5 workflow types** — feature, bugfix, review, small, directive — each with a tailored step sequence (skip design for bugfixes, skip implementation for reviews)
- **Discovery mode** — multi-turn requirement exploration when you have a vague idea but need to clarify before coding
- **Loop mode with git worktree isolation** — continuous autonomous execution on an isolated branch, with `--merge` to bring changes back safely
- **Spec-driven guardrails** — existing requirements cannot be silently weakened or deleted; the engine enforces spec integrity
- **Smart version bumping** — LLM-analyzed semantic versioning that determines patch/minor/major from actual code changes
- **Confirmation/review steps** — insert human or LLM review gates after propose/design steps
- **Multi-language support** — configure output language (e.g., zh-CN) independently from spec language (e.g., en-US)

## Quick Start

### 1. Install

```bash
# From the SE3 repository root
pip install -e .
```

### 2. Initialize a project

```bash
cd your-project
se3 init
```

This creates:
```
se3/
├── specs/
│   └── base/
│       └── spec.md    # Base project specification (edit this)
se3.yaml               # Framework configuration
```

### 3. Run a task

```bash
se3 run "Add user authentication with JWT tokens"
```

The engine will: analyze the task → read relevant specs → propose a solution → design the architecture → plan implementation tasks → write code → run tests → verify spec compliance → update specs → bump version → commit → generate a summary.

## Usage

### New Task

```bash
# Run with automatic workflow type detection
se3 run "Fix the login timeout bug"

# Specify the workflow type explicitly
se3 run --type bugfix "Fix the login timeout bug"
se3 run --type feature "Add OAuth2 support"
se3 run --type small "Fix typo in error message"
```

### Resume an Interrupted Flow

```bash
# Resume the most recent interrupted flow
se3 run --resume

# Resume a specific flow by ID
se3 run --resume --flow-id <flow-id>
```

### Discovery Mode

When you have a vague idea and need to explore requirements:

```bash
se3 run --discover "I need something for user role management"
```

Discovery mode conducts a multi-turn conversation to clarify requirements, then proceeds with the full workflow using the refined description.

### Loop Mode (Continuous Autonomous Execution)

Loop mode runs multiple tasks autonomously, each on an isolated git worktree branch:

```bash
# Start loop mode (default: up to 10 iterations)
se3 run --loop

# Limit iterations
se3 run --loop --max-iterations 5

# Disable branch isolation (work directly on current branch)
se3 run --loop --no-worktree
```

Managing loop branches:

```bash
# List all unmerged loop branches
se3 run --list-loops

# Merge a loop branch back (shows diff summary first)
se3 run --merge se3-loop/20260324-120000
```

### Other Commands

```bash
# Check spec files against guardrails
se3 guardrails se3/specs/auth/spec.md

# View session history
se3 history
```

### Daemon & Central Server

SE3 has an optional always-on control plane. The **daemon** (`se3 daemon`)
runs resident on a machine, supervising its `se3 run` flows and aggregating
their state; it can dial out to a **central server** (`se3-server`) that merges
many machines into one multi-flow view and serves a web frontend for watching
progress and publishing tasks remotely.

```bash
# Install the optional server extra (FastAPI/uvicorn/websockets)
pip install 'se3[server]'

# Start the resident daemon (detached background process)
se3 daemon start

# Start the central server (defaults to 127.0.0.1:8080)
se3-server
```

See [docs/daemon-and-server.md](docs/daemon-and-server.md) for installation,
deployment, architecture, and the web frontend.

## Workflow Types

SE3 adapts its process to the type of work. Each workflow type selects a different subset of the 11-step engine:

| Type | Steps | When to Use |
|------|-------|-------------|
| **feature** | analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → version_analyze → commit → summarize | New functionality or significant enhancements |
| **bugfix** | analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → version_analyze → commit → summarize | Bug fixes (skips design for faster iteration) |
| **directive** | analyze → read_spec → plan_tasks → implement → test → verify_spec → version_analyze → commit → summarize | Following specific instructions (skips propose + design) |
| **small** | analyze → implement → test → version_analyze → commit → summarize | Typos, minor fixes, simple changes |
| **review** | analyze → read_spec → verify_spec → summarize | Code review, audit, or analysis (no implementation) |

Discovery mode (`--discover`) prepends a **discovery** step to any workflow type for multi-turn requirement exploration.

## Architecture Overview

### The 11-Step Flow Engine

SE3's core is a program-driven state machine. Each step has a clear responsibility:

| # | Step | What It Does | Automated? |
|---|------|-------------|------------|
| 1 | **analyze** | Classify task type and scope | LLM |
| 2 | **read_spec** | Load relevant specification files | Automated |
| 3 | **propose** | Generate a change proposal identifying affected files | LLM |
| 4 | **design** | Architect the solution with design document | LLM |
| 5 | **plan_tasks** | Break into concrete tasks | LLM |
| 6 | **implement** | Write code, declare tests added | LLM |
| 7 | **test** | Run test suite, trigger fix loop on failure | Automated |
| 8 | **verify_spec** | Check implementation against spec requirements | LLM |
| 9 | **update_spec** | Update specs with guardrail enforcement | LLM |
| 10 | **version_analyze** | Determine SemVer bump from actual changes | LLM |
| 11 | **commit** | Stage, version bump, and commit | Automated |

A final **summarize** step generates a handoff summary for the next session.

### State Persistence

All flow state is persisted to `se3/state/engine.json`, enabling:
- **Resume** — pick up exactly where you left off after interruption
- **History** — full audit trail of every step's inputs and outputs
- **Fix loops** — when tests fail, the engine returns to implement automatically

### LLM Subprocess Pattern

Steps that require "thinking" (analyze, propose, design, etc.) spawn an LLM subprocess with carefully constructed context. The subprocess receives only the information relevant to that step — not the entire conversation history. This keeps each step focused and prevents context pollution.

## Project Structure

```
project/
├── se3.yaml                    # Framework configuration
├── pyproject.toml              # Python project config
├── se3/                        # SE3 runtime directory (gitignored except specs/)
│   ├── specs/                  # Source of truth for requirements (committed)
│   │   ├── base/
│   │   │   └── spec.md         # Base project conventions (required)
│   │   └── [capability]/
│   │       └── spec.md         # Feature/domain specifications
│   ├── state/                  # Flow engine state persistence
│   ├── history/                # LLM chat history (NDJSON)
│   ├── cache/                  # Cache files
│   ├── logs/                   # Execution logs
│   ├── calls/                  # Human approval call queue
│   └── collab/                 # Multi-agent collaboration state
├── src/                        # Source code
├── tests/                      # Test files
└── scripts/                    # Helper scripts
```

## Configuration

SE3 is configured via `se3.yaml` in the project root. Key sections:

```yaml
# Confirmation/review gates
confirmation:
  enabled: true
  steps: ["propose", "design"]    # Insert review after these steps
  reviewer: "human"               # "human" or "llm"
  llm_reviewer:
    model: null
    max_iterations: 3

# Automatic version bumping
# version_analyze (LLM) decides the new version directly; default rules are
# SemVer 2.0.0. To customize, create se3/version-rules.md (natural-language
# Markdown) — its contents are injected into the version_analyze prompt.
version:
  enabled: true
  auto_bump: true

# Language settings
language:
  language: zh-CN                 # Human-facing outputs
  spec_language: en-US            # Spec writing language

# Claude command resolution (priority-based fallback)
claude_commands:
  - cmd: "claude"
    priority: 10
```

## Problems Solved

| Problem | How SE3 Solves It |
|---------|-------------------|
| **Context loss between sessions** | State persistence in `se3/state/` + summarize step generates handoff notes; `--resume` picks up exactly where you left off |
| **No engineering discipline** | The flow engine enforces analysis → design → implementation → testing in order; you can't skip to code on a feature task |
| **Spec drift** | Guardrails prevent silently weakening or deleting existing requirements; `update_spec` step explicitly manages spec changes |
| **Unverified work** | `test` step runs automatically; `verify_spec` checks implementation against requirements; failures trigger fix loops |
| **One-size-fits-all process** | 5 workflow types adapt the process: full ceremony for features, fast path for bugfixes, read-only for reviews |
| **Manual workflow orchestration** | `se3 run` handles everything: no manual step tracking, no remembering "what's next" |
| **Unsafe autonomous execution** | Loop mode uses git worktree isolation; changes stay on a separate branch until explicitly merged |
| **Version management overhead** | Smart version analysis determines the right SemVer bump automatically from actual code changes |
| **Vague requirements** | Discovery mode conducts multi-turn exploration before committing to implementation |

## Version History

See [VERSIONS.md](VERSIONS.md) for the complete version history.

**Current Version: 3.38.1**

## License

Apache-2.0
