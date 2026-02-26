# Spec Updates for SE3 Run (Flow Engine)

**Date:** 2026-02-26

## Summary

Updated SE3 specs to reflect the new Flow Engine architecture with `se3 run` as the unified entry point. The traditional start/work/done workflow has been replaced by a state machine-driven 11-step process.

## Changes

### 1. New Spec: flow-engine

**Location:** `se3/specs/flow-engine/spec.md`

Created new spec defining the core Flow Engine:
- Unified `se3 run` entry point
- 11-step state machine (analyze, read_spec, propose, design, plan_tasks, implement, test, verify_spec, update_spec, commit, summarize)
- State persistence and resume capability
- Step input/output passing
- Error handling and retry
- Ctrl+C interrupt with prompt injection

### 2. Updated: se3-commands

**Location:** `se3/specs/se3-commands/spec.md`

Updated to reflect new command structure:
- `se3 run` as primary entry point (replacing se3:start, se3:work, se3:done)
- Legacy command deprecation notice
- Updated `se3 status` to include flow information
- Added `--resume` and `--loop` options
- Task type mapping (feature, bugfix, review, small, directive)

### 3. Updated: se3-workflows

**Location:** `se3/specs/se3-workflows/spec.md`

Updated to match 11-step Flow Engine:
- Defined 5 workflow types with different step sequences
- Documented each of the 11 steps in detail
- Updated adaptive formality rules
- Added step retry and recovery requirements
- Changed workflow entry to `se3 run`

### 4. Updated: session-protocol

**Location:** `se3/specs/session-protocol/spec.md`

Updated for Flow Engine startup:
- Unified startup via `se3 run`
- Resume capability with `--resume`
- Updated input classification for step routing
- State persistence in `se3/state/engine.json`
- Added loop mode requirements
- Removed legacy se3:start/se3:done references

### 5. Updated: se3-scaffold

**Location:** `se3/specs/se3-scaffold/spec.md`

Updated project structure:
- Added `se3/state/engine.json` for Flow Engine state
- Removed `openspec/` as primary spec location
- `specs/` is now the primary spec directory
- Updated CLI tools list to include `se3 run`
- Changed change lifecycle to flow-based tracking
- Added Flow Engine to self-iterate requirements

## Backward Compatibility

- Legacy commands (`se3:start`, `se3:work`, `se3:done`) are deprecated but may still work
- `openspec/specs/` is still supported as fallback for spec discovery
- Existing `openspec/changes/` directories are preserved but new flows use Flow Engine

## Migration Notes

For projects using legacy workflow:
1. Continue using existing changes in `openspec/changes/`
2. New work should use `se3 run` for unified entry
3. Run `se3 migrate` to update directory structure if needed
4. Specs can remain in `openspec/specs/` or be moved to `specs/`
