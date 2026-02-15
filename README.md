# SE 3.0 — Software Engineering 3.0 Framework

An AI-first long-horizon development framework for Claude Code.

## Overview

SE 3.0 combines Anthropic's [long-running agent best practices](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) with Spec Driven Development ([OpenSpec](https://github.com/Fission-AI/OpenSpec)) into a coherent system where AI agents drive development autonomously, calling humans only when needed.

### Core Principles

- **Human-as-MCP**: All human input (including initial project intent) obtained on-demand via human calls. Sync (ask directly) or async (write file). No pre-written requirement files.
- **Specs as Truth**: OpenSpec specs are the source of truth for **requirements**. In agent team mode, specs are contracts between agents.
- **Adaptive Formality**: Full openspec workflow for large changes; skip ceremony for small ones. Match process to scope.
- **Progressive Loading**: Sessions start with `progress.md` + `git log`. Everything else loads on demand.
- **Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria.
- **Spec Guardrails**: Agents MUST NOT weaken or delete existing requirements without human approval.
- **Adaptive Conventions**: Commit when work is meaningful, clear context when saturated — not on mechanical schedules.
- **Native Agent Team**: Multi-agent via Claude Code's built-in Task tool. Specs serve as shared context for sub-agents.

## Quick Start

### 1. Install SE 3.0 CLI

```bash
enter tools
pip install -e .
```

### 2. Initialize a project

```bash
mkdir my-project && cd my-project
git init
se3 init
```

This creates:

```
.claude/
├── CLAUDE.md    # Project-specific conventions (~25 lines)
└── SE3.md       # Complete SE 3.0 framework (~300 lines, managed by se3)
```

### 3. Start developing

Tell Claude Code: `self-iterate`

The agent will:
1. Read `.claude/CLAUDE.md` for project configuration
2. Read `.claude/SE3.md` for framework rules
3. Detect an empty project → ask "What should this project do?" (human call)
4. Create an openspec change from your answer (proposal = intent, specs = formalization)
5. Implement incrementally, calling you only when it needs decisions or actions

### 4. Maintain SE3.md

```bash
# Check if SE3.md is up to date
se3 doctor

# Update to latest SE3 version
se3 upgrade
```

## Project Structure

```
project/
├── init.sh                 # Environment setup (optional)
├── status.md               # Runtime dashboard (current session state)
├── progress.md             # Cross-session history
├── se3.config.yaml         # Configuration (optional)
├── README.md
├── human-calls/            # Async human call queue
├── tests/                  # Test files
├── tools/                  # CLI tools (se3 command)
├── .e2e-baselines/         # Visual regression baselines (optional)
├── openspec/
│   ├── specs/              # Source of truth for requirements
│   └── changes/
│       └── archive/
└── .claude/
    ├── CLAUDE.md           # Project-specific conventions (editable)
    └── SE3.md              # SE 3.0 framework reference (managed by se3)
```

## SE 3.0 CLI Tools

SE 3.0 includes CLI tools to validate and enforce framework conventions:

```bash
# Install tools
pip install se3

# Initialize a new SE 3.0 project
se3 init

# Validate specs
se3 lint

# Sync output/ directory with source
se3 sync --dry-run   # Preview changes
se3 sync --apply     # Apply changes

# Verify change implementation
se3 verify --change <change-name>

# Diagnose session state
se3 status

# Check SE3.md health
se3 doctor

# Update SE3.md to latest version
se3 upgrade
```

See `output/TOOLS.md` for detailed documentation.

## Key Concepts

### Flow

```
human call → openspec change (proposal → specs → code) → archive updates main specs
```

Proposal captures intent, specs formalize it, archives preserve history. For small changes, skip the openspec workflow entirely — edit code, update spec if behavior changed, commit.

### Session Protocol

Progressive startup: `status.md` (current state) → `progress.md` (history) + `git log` → determine scope → load more only as needed. First-time: ask the human via human call.

### Human-as-MCP

| Mode | When | How |
|------|------|-----|
| Sync | Human present | Ask directly (AskUserQuestion) |
| Async | Human absent / offline action needed | Write to `human-calls/` |

### SE3.md Module System

SE 3.0 uses a two-file architecture to separate framework conventions from project-specific configurations:

| File | Purpose | Editable | Managed by |
|------|---------|----------|------------|
| `.claude/SE3.md` | Complete SE 3.0 framework specification | ❌ No | `se3` CLI |
| `.claude/CLAUDE.md` | Project-specific conventions and overrides | ✅ Yes | Project team |

This separation allows:
- **Framework updates**: `se3 upgrade` updates SE3.md without touching project-specific configurations
- **Minimal project setup**: CLAUDE.md can be as short as 25 lines for simple projects
- **Version consistency**: SE3.md includes version and checksum for validation

### Adaptive Conventions

- **Commit**: When meaningful work is done. Not tied to /new.
- **Context clear**: When saturated or switching tasks. Not after every task group.

### Agent Team

Native Task tool. Parent spawns sub-agents per openspec change. Specs on the file system serve as contracts — sub-agents read them to know what to implement. Results return directly.

## Output Files

| File | Purpose |
|------|---------|
| `output/SE3.md.template` | SE3 framework template → `.claude/SE3.md` (via `se3 init`) |
| `output/CLAUDE.minimal.md.template` | Minimal CLAUDE.md template (25 lines) |
| `output/status.md.template` | Session status template → project root `status.md` |
| `output/TOOLS.md` | CLI tools documentation |
| `docs/best-practices.md` | Best practices guide |

## Version History

### SE3 Framework Version

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2026-02-16 | Initial stable release: `se3 init`, `se3 update`, `se3 commit`, `se3 collab`, Semantic Versioning 2.0.0 |

<details>
<summary>Pre-1.0 Development History (archived)</summary>

- v7.0 — 2026-02-14 — SE3 Module System: Separate framework (SE3.md) from project config (CLAUDE.md), `se3 init` command
- v6.1 — 2026-02-14 — Requirement intake: three-source taxonomy for structured requirement capture
- v6.0 — 2026-02-14 — CLI tools: se3 lint, sync, verify, status for enforceable framework
- v5.1 — 2026-02-14 — Diagnostic dashboard: status.md for single-source-of-truth session state
- v5.0 — 2026-02-14 — Verification protocol, spec guardrails, init.sh environment automation
- v4.1 — 2026-02-14 — Adaptive formality: match SDD ceremony to change scope
- v4.0 — 2026-02-14 — Remove demands.md, specs as truth, adaptive commit/context rules
- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial concept

</details>
<!-- Test comment from collab at 2026-02-16T00:31:59+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:31:59+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:31:59+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:03+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:03+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:04+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:15+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:15+08:00 -->
<!-- Test comment from collab at 2026-02-16T00:32:15+08:00 -->
