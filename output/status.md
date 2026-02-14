# Session Status

> Single source of truth for current session state.
> Auto-updated by agent. Human reads this first to diagnose issues.

## Current State

**Active Change**: `none` *(name of openspec change, or "none")*
**Current Task**: `idle` *(task ID or "idle")*
**Status**: `ready` *(ready / blocked / error / waiting-human)*
**Blocked Since**: `-` *(timestamp if blocked)*
**Context Budget**: `fresh` *(fresh / moderate / saturated)*

## What's Happening Now

*One-line summary of current activity*

## Immediate Next Steps

1. *(highest priority action)*
2. *(fallback if #1 blocked)*
3. *(if all blocked, issue human call)*

## Blockers

| Issue | Type | Since | Resolution |
|-------|------|-------|------------|
| *none* | - | - | - |

## Recent History (last 3 actions)

1. `[timestamp]` - *action taken*
2. `[timestamp]` - *action taken*
3. `[timestamp]` - *action taken*

## Quick Diagnosis

If agent appears stuck, check:
1. `Active Change` above — should match `openspec/changes/`
2. `Blockers` table — any unresolved issues?
3. `human-calls/` — pending async calls?
4. `git status` — uncommitted work?
5. Latest `progress.md` entry — context from previous session

---

**File Location**: Project root `status.md`
**Update Frequency**: After every significant action or state change
**Archive**: Overwritten each session (not versioned — use git for history)
