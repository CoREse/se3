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

### 1. Global conventions (one-time)

```bash
cp output/CLAUDE.global.md ~/.claude/CLAUDE.md
```

### 2. Initialize a project

```bash
mkdir my-project && cd my-project
git init
openspec init --tools claude
mkdir -p human-calls
cp path/to/se3.0/output/CLAUDE.md .claude/CLAUDE.md
```

### 3. Start developing

Tell Claude Code: `self-iterate`

The agent will:
1. Detect an empty project → ask "What should this project do?" (human call)
2. Create an openspec change from your answer (proposal = intent, specs = formalization)
3. Implement incrementally, calling you only when it needs decisions or actions

### 4. Respond to async human calls

Check `human-calls/` for pending requests. Fill in `## Response`.

## Project Structure

```
project/
├── init.sh                 # Environment setup (optional)
├── status.md               # Runtime dashboard (current session state)
├── progress.md             # Cross-session history
├── se3.config.yaml         # Configuration (optional)
├── README.md
├── human-calls/            # Async human call queue
├── .e2e-baselines/         # Visual regression baselines (optional)
├── openspec/
│   ├── specs/              # Source of truth for requirements
│   └── changes/
│       └── archive/
└── .claude/
    └── CLAUDE.md            # SE 3.0 project config
```

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

### Adaptive Conventions

- **Commit**: When meaningful work is done. Not tied to /new.
- **Context clear**: When saturated or switching tasks. Not after every task group.

### Agent Team

Native Task tool. Parent spawns sub-agents per openspec change. Specs on the file system serve as contracts — sub-agents read them to know what to implement. Results return directly.

## Output Files

| File | Purpose |
|------|---------|
| `output/CLAUDE.md` | Project-level template → `.claude/CLAUDE.md` |
| `output/CLAUDE.global.md` | Global conventions → `~/.claude/CLAUDE.md` |
| `output/se3.config.yaml` | Configuration template |
| `output/status.md` | Session status template → project root `status.md` |
| `output/TOOLS.md` | CLI tools documentation |
| `docs/best-practices.md` | Best practices guide |

## SE 3.0 CLI Tools

SE 3.0 includes CLI tools to validate and enforce framework conventions:

```bash
# Install tools
cd tools/ && pip install -e .

# Validate specs
se3 lint

# Sync output/ directory with source
se3 sync --dry-run   # Preview changes
se3 sync --apply     # Apply changes

# Verify change implementation
se3 verify --change <change-name>

# Diagnose session state
se3 status
```

See `output/TOOLS.md` for detailed documentation.

## Version History

- v5.1 — 2026-02-14 — Diagnostic dashboard: status.md for single-source-of-truth session state
- v5.0 — 2026-02-14 — Verification protocol, spec guardrails, init.sh environment automation
- v4.1 — 2026-02-14 — Adaptive formality: match SDD ceremony to change scope, specs as agent contracts
- v4.0 — 2026-02-14 — Remove demands.md, specs as truth, adaptive commit/context rules
- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial version
