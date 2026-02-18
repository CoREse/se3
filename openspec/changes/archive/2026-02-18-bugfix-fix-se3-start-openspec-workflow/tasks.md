## Tasks

- [x] 1. Analyze the bug: Compare session-protocol spec with se3:start skill
- [x] 2. Create proposal documenting the gap
- [x] 3. Fix output/commands/se3/start.md: Add spec loading step
- [x] 4. Fix output/commands/se3/start.md: Add source-of-truth guardrail
- [x] 5. Verify fix by reading updated skill
- [x] 6. Run se3 lint to validate output specs

## Summary

Fixed `se3:start` skill to properly treat OpenSpec as single source of truth:

1. Added Step 3: "Load relevant specifications" with explicit spec loading instructions
2. Added spec listing command: `openspec list --specs`
3. Added spec reading instruction: read related specs when active changes exist
4. Added explicit statement: "specs are the single source of truth"
5. Added guardrail: "Agent MUST NOT deviate from spec requirements without explicit human approval"
6. Updated summary reporting to include "Relevant specs that will govern the work"
7. Added guardrails: "Specs are authoritative" and "On-demand spec loading"

The pre-existing lint error in git-worktree-collab/spec.md is unrelated to this change.
