# SE 3.0 — Software Engineering 3.0 Framework

An AI-first long-horizon development framework for Claude Code.

## Overview

SE 3.0 combines Anthropic's [long-running agent best practices](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) with Spec Driven Development ([OpenSpec](https://github.com/Fission-AI/OpenSpec)) into a coherent development system.

### Core Principles

- **Human-as-MCP**: All human input (including project intent) enters through on-demand human calls — sync (ask directly) or async (write file). No pre-written intent files.
- **Progressive Loading**: Sessions start by reading only `progress.md` + `git log`. Everything else loads on demand.
- **Incremental Development**: Work in openspec changes. Each session stays within a bounded scope.
- **Native Agent Team**: Multi-agent work uses Claude Code's built-in Task tool. No custom communication layer.

## Quick Start

### 1. Set up global conventions

```bash
cp output/CLAUDE.global.md ~/.claude/CLAUDE.md
```

### 2. Initialize a new project

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
1. Detect an empty project → ask you "What should this project do?" (human call)
2. Write your answer into `demands.md`
3. Implement requirements incrementally through openspec changes
4. Ask you when it needs decisions, information, or actions (human call)

### 4. Respond to async human calls

Check `human-calls/` for pending requests. Fill in the `## Response` section.

## Project Structure

```
project/
├── demands.md             # Requirements (obtained via human calls)
├── progress.md            # Cross-session progress
├── se3.config.yaml        # Configuration (optional)
├── README.md
├── human-calls/           # Async human call queue
├── openspec/
│   ├── specs/
│   ├── changes/
│   └── archive/
└── .claude/
    └── CLAUDE.md           # SE 3.0 project-level config
```

## Output Files

| File | Purpose |
|------|---------|
| `output/CLAUDE.md` | Project-level SE 3.0 template → `.claude/CLAUDE.md` |
| `output/CLAUDE.global.md` | Global conventions template → `~/.claude/CLAUDE.md` |
| `output/se3.config.yaml` | Configuration template |
| `docs/best-practices.md` | Best practices guide |

## Key Concepts

### Session Protocol

Progressive startup: read `progress.md` latest entry + `git log` → determine scope → load more only as needed. First-time: ask the human what to build via human call.

### Human-as-MCP

| Mode | When | How |
|------|------|-----|
| Sync | Human is present | Ask directly (AskUserQuestion) |
| Async | Human unavailable / needs offline action | Write to `human-calls/` |

Types: `decision` (choose between options), `action` (do something offline), `information` (provide knowledge).

### Agent Team

Uses Claude Code's native Task tool. Parent spawns sub-agents, each working on a separate openspec change. No file-based communication — results return directly through the Task tool.

### SDD Flow

`demands.md` → openspec specs → openspec changes → code

## Version History

- v3.0 — 2026-02-14 — English rewrite, native agent team, global CLAUDE.md, remove se3-init
- v2.0 — 2026-02-14 — Remove intentions.md, unified Human-as-MCP, progressive startup
- v1.0 — 2026-02-14 — Initial version
