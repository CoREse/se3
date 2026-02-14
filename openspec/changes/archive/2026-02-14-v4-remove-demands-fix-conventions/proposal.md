## Why

demands.md is redundant with openspec specs — it creates a synchronization problem and adds a maintenance layer with no unique value. The openspec change proposal already captures "demands", and specs accumulate as the source of truth.

Additionally, three conventions from the original CLAUDE.md are too rigid: (1) mandatory commit before every /new, (2) mandatory /new after every task group, (3) case sensitivity reminder that wastes space.

## What Changes

- Remove demands.md from the framework. Human calls drive openspec changes directly, specs are the source of truth.
- Redesign self-iterate flow: human call → openspec change → implement → verify → repeat
- Fix commit convention: commit at meaningful work units, not tied to /new
- Fix context clearing: /new when context is saturated, not mechanically per task group
- Remove case sensitivity reminder
- Update both CLAUDE.md templates and all documentation

## Capabilities

### Modified Capabilities
- `session-protocol`: Update shutdown to remove demands.md references, fix commit convention
- `se3-scaffold`: Remove demands.md from project structure, update self-iterate flow
- `human-as-mcp`: Update first-bootstrap to create openspec change directly instead of demands.md

## Impact

- demands.md removed from project structure and all references
- Self-iterate flow simplified
- Commit and /new conventions made adaptive instead of rigid
- Both output/CLAUDE.md and output/CLAUDE.global.md updated
