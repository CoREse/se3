# Progress

## 2026-02-14 Session 2 (v5.0)

### Done
- Added verification protocol to CLAUDE.md: "never mark complete without tests", baseline testing at startup, tests before commit
- Added spec guardrails to CLAUDE.md and CLAUDE.global.md: agents MUST NOT delete/weaken requirements, implementers MUST NOT modify the spec they implement against
- Added init.sh to startup protocol (Step 0) and project structure
- Updated best-practices.md with verification, guardrails, and init.sh sections
- Updated README.md with new principles and version history

### Changes
- `v5-testing-guardrails-init`: completed

### Open Issues
- None

### Next Steps
- Test SE 3.0 v5.0 in a real project
- Validate verification protocol catches under-tested completions
- Validate spec guardrails prevent requirement drift

## 2026-02-14 Session 1 (v4.1)

### Done
- Added adaptive formality guidance to SDD section of CLAUDE.md
  - Large changes: full openspec workflow (proposal → specs → design → tasks)
  - Medium: brief proposal + tasks, specs if needed, skip design
  - Small: no openspec change, edit directly, update spec if behavior changed
- Positioned specs as agent contracts in agent team mode
- Added concrete role prompts for architect/implementer/reviewer
- Updated CLAUDE.global.md, README.md, best-practices.md

### Changes
- All previous changes archived
- v4.1 applied directly (small change, no openspec change needed — practicing what we preach)

### Open Issues
- None

### Next Steps
- Test SE 3.0 v4.1 in a real project
- Validate agent team workflow with actual Task tool sub-agents
