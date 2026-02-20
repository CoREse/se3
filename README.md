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
cd tools
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

Use the SE3 workflow skills:

```
/se3:start   # Initialize session, check environment, load context
/se3:work    # Start or continue working on a change
/se3:done    # End session: tests, commit, handoff
```

The agent will:
1. Run `se3 start --json` to compute session state and actions
2. Execute the returned actions (environment setup, baseline tests, etc.)
3. For `/se3:work`: Guide through bugfix/feature/review workflows with step tracking
4. For `/se3:done`: Run tests, commit changes, generate session summary

All workflow logic is encoded in CLI commands that return JSON action arrays — the agent follows the program, not prose.

### 4. Maintain SE3.md

```bash
# Update to latest SE3 version
se3 update
```

## Project Structure

```
project/
├── init.sh                 # Environment setup (optional)
├── progress.md             # Cross-session history (auto-maintained by SE3 tools)
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
    ├── SE3.md              # SE 3.0 framework reference (managed by se3)
    └── commands/se3/       # Workflow skills: start.md, work.md, done.md
```

## SE 3.0 CLI Tools

SE 3.0 includes CLI tools to validate and enforce framework conventions:

```bash
# Install tools
cd tools && pip install -e .

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

# Workflow driver commands (return JSON actions arrays)
se3 start --json       # Session initialization
se3 work --json        # List active changes or continue work
se3 work feature/auth --json   # Work on specific change
se3 done --json        # Session shutdown

# Update SE3.md to latest version
se3 update

# Commit with test verification
se3 commit -m "Add feature"

# Multi-agent collaboration
se3 collab --daemon "Implement feature"

# Show Claude command resolution
se3 claude-cmd
```

See `output/TOOLS.md` for detailed documentation.

## Key Concepts

### Flow

```
human call → openspec change (proposal → specs → code) → archive updates main specs
```

Proposal captures intent, specs formalize it, archives preserve history. For small changes, skip the openspec workflow entirely — edit code, update spec if behavior changed, commit.

### Session Protocol

Progressive startup: `se3 status` (computed state) → `progress.md` (history) + `git log` → determine scope → load more only as needed. First-time: ask the human via human call.

### Human-as-MCP

| Mode | When | How |
|------|------|-----|
| Sync | Human present | Ask directly (AskUserQuestion) |
| Async | Human absent / offline action needed | Write to `human-calls/` |

### SE3.md Module System

SE 3.0 uses a two-file architecture to separate framework conventions from project-specific configurations:

| File | Purpose | Editable | Managed by |
|------|---------|----------|------------|
| `.claude/SE3.md` | Complete SE 3.0 framework specification | No | `se3` CLI |
| `.claude/CLAUDE.md` | Project-specific conventions and overrides | Yes | Project team |

This separation allows:
- **Framework updates**: `se3 update` updates SE3.md without touching project-specific configurations
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
| `output/TOOLS.md` | CLI tools documentation |
| `docs/best-practices.md` | Best practices guide |

## Version History

See [VERSIONS.md](VERSIONS.md) for the complete version history.

**Current Version: 2.22.4**

When modifying framework files:
1. Bump `SE3_FRAMEWORK_VERSION` in `tools/se3_tools/__init__.py`
2. Add entry to `VERSIONS.md`
3. Update `README.md` version reference
4. All are enforced by `se3 commit` (blocking check)
