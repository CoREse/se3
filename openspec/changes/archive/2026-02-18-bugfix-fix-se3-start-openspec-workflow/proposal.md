# Proposal: Fix se3:start OpenSpec Workflow

## Problem

Current `/se3:start` skill does not treat OpenSpec as the "single source of truth":

1. **Missing spec loading**: Skill checks `openspec/changes/` for active changes but does not read `openspec/specs/` for relevant specifications
2. **No source-of-truth concept**: Skill description never mentions that specs are the authoritative source of requirements
3. **Incomplete scope determination**: Step 6 says "determine scope based on progress + active changes" but omits the critical "+ relevant specs" component

## Evidence

From `session-protocol` spec (the authoritative source):
> Step 5: Check `openspec/changes/` for active changes and **`openspec/specs/` for current capabilities**

From `se3-scaffold` spec:
> OpenSpec specs serve as the **single source of truth** for project requirements.

From `best-practices.md`:
> OpenSpec specs are the **source of truth for requirements** — in agent team mode, specs are **contracts between agents**

## Solution

Update `/se3:start` skill (`output/commands/se3/start.md`) to:

1. **Add spec discovery step**: After checking active changes, list available specs via `openspec list --specs`
2. **Add spec reading step**: When active changes exist, read the related spec files before transitioning to work mode
3. **Add source-of-truth guardrail**: Remind agent that specs are authoritative and implementation must follow spec requirements
4. **Update scope determination**: Clarify that scope is determined from "progress + active changes + relevant specs"

## Impact

- **Correctness**: Ensures agent understands project capabilities before starting work
- **Consistency**: Aligns skill behavior with SE3 principles (specs as contracts)
- **Multi-agent safety**: Prevents agents from deviating from agreed specifications
