## Context

v3 still carries demands.md and rigid commit/context-clearing rules inherited from the original CLAUDE.md. This change removes them.

## Goals / Non-Goals

**Goals:**
- Remove demands.md — specs are the single source of truth
- Human call results drive openspec changes directly
- Commit at meaningful work units, not tied to /new
- Clear context when saturated, not mechanically per task group

**Non-Goals:**
- No changes to human-as-MCP sync/async mechanism
- No changes to progressive startup logic (just remove demands.md references)

## Decisions

### D1: Specs replace demands.md

The flow becomes: `human call → openspec change proposal → specs → code`

The proposal IS the demand. Once implemented and archived, specs are the record. README provides the overview. No middle layer needed.

### D2: Adaptive commit and context rules

Old: "commit before every /new" + "/new after every task group"
New: "commit when meaningful work is done" + "/new when context is saturated or switching tasks"

This matches how experienced developers actually work — you commit when something is done, not on a timer.
