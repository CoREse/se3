# SE 3.0 — Software Engineering 3.0 Framework

An AI-first long-horizon development framework for Claude Code.

## Overview

SE 3.0 combines Anthropic's [long-running agent best practices](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) with Spec Driven Development ([OpenSpec](https://github.com/Fission-AI/OpenSpec)) into a coherent system where AI agents drive development autonomously, calling humans only when needed.

### Core Principles

- **Human-as-MCP**: All human input (including initial project intent) obtained on-demand via human calls. Sync (ask directly) or async (write file). No pre-written requirement files.
- **Specs as Truth**: OpenSpec specs are the single source of truth. No intermediate demands/requirements layer.
- **Progressive Loading**: Sessions start with `progress.md` + `git log`. Everything else loads on demand.
- **Adaptive Conventions**: Commit when work is meaningful, clear context when saturated — not on mechanical schedules.
- **Native Agent Team**: Multi-agent via Claude Code's built-in Task tool. No custom communication layer.

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
├── progress.md             # Cross-session progress
├── se3.config.yaml         # Configuration (optional)
├── README.md
├── human-calls/            # Async human call queue
├── openspec/
│   ├── specs/              # Source of truth for requirements
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md            # SE 3.0 project config
```

## Key Concepts

### Flow

```
human call → openspec change (proposal = demand) → specs → code
```

No intermediate requirements file. The proposal captures what's needed; specs formalize it; archived changes are the historical record.

### Session Protocol

Progressive startup: `progress.md` latest entry + `git log` → determine scope → load more only as needed. First-time: ask the human via human call.

### Human-as-MCP

| Mode | When | How |
|------|------|-----|
| Sync | Human present | Ask directly (AskUserQuestion) |
| Async | Human absent / offline action needed | Write to `human-calls/` |

### Adaptive Conventions

- **Commit**: When meaningful work is done. Not tied to /new.
- **Context clear**: When saturated or switching tasks. Not after every task group.

### Agent Team

Native Task tool. Parent spawns sub-agents per openspec change. Results return directly.

## Output Files

| File | Purpose |
|------|---------|
| `output/CLAUDE.md` | Project-level template → `.claude/CLAUDE.md` |
| `output/CLAUDE.global.md` | Global conventions → `~/.claude/CLAUDE.md` |
| `output/se3.config.yaml` | Configuration template |
| `docs/best-practices.md` | Best practices guide |

## Version History

- v4.0 — 2026-02-14 — Remove demands.md, specs as truth, adaptive commit/context rules
- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial version
