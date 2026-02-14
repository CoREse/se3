## Context

v2 has three issues: Chinese CLAUDE.md wastes tokens, agent team doesn't use native Claude Code mechanisms, and se3-init is redundant. User also requested a global CLAUDE.md for universal conventions.

## Goals / Non-Goals

**Goals:**
- Rewrite CLAUDE.md in English
- Align agent team with Claude Code's Task tool
- Produce global CLAUDE.md for ~/.claude/
- Remove se3-init and agent-comms/

**Non-Goals:**
- No changes to session protocol logic (already redesigned in v2)
- No changes to human-as-mcp mechanism
- No changes to SDD/openspec workflow

## Decisions

### D1: English for all framework files

All framework output files (CLAUDE.md, config, docs) in English. Chinese saves zero tokens and hurts adherence. Project-specific content (demands.md written by Chinese-speaking users) can remain in whatever language the user prefers.

### D2: Native Task tool for agent team

Claude Code's Task tool spawns sub-agents that share the file system and return results directly. This eliminates agent-comms/ entirely. Role differentiation happens through the prompt passed to the Task tool, not through config files.

### D3: Two-tier CLAUDE.md

- **Global** (~/.claude/CLAUDE.md): Universal conventions — commit standards, SDD basics, case sensitivity, documentation rules. These apply to ALL projects.
- **Project** (.claude/CLAUDE.md): SE 3.0 specific — session protocol, human-as-MCP, agent team, project structure. Only for SE 3.0 projects.
