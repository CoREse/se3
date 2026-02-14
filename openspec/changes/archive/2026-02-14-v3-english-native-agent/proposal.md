## Why

Three issues identified in v2: (1) CLAUDE.md is in Chinese, costing 2-3x tokens and reducing instruction adherence; (2) agent team design uses custom file-based comms instead of Claude Code's native Task tool; (3) se3-init skill is redundant with the startup protocol. Additionally, need a separate global CLAUDE.md for universal conventions.

## What Changes

- Rewrite output/CLAUDE.md entirely in English for token efficiency and better LLM adherence
- Remove se3-init skill — startup protocol handles initialization
- Redesign agent team to use Claude Code's native Task tool (sub-agents via `subagent_type`)
- Remove `agent-comms/` directory — native Task tool returns results directly
- Create output/CLAUDE.global.md for `~/.claude/CLAUDE.md` with universal conventions
- Update all documentation to English

## Capabilities

### New Capabilities

### Modified Capabilities
- `agent-team`: Replace file-based comms with Claude Code native Task tool mechanism
- `se3-scaffold`: Remove agent-comms/, add global CLAUDE.md output
- `se3-init-skill`: Remove entirely — redundant with startup protocol

## Impact

- output/CLAUDE.md fully rewritten in English
- output/CLAUDE.global.md created (new file)
- output/skills/se3-init/ removed
- agent-comms/ concept removed from framework
- README.md and best-practices.md updated
