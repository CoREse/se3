# Global Conventions

> Place at `~/.claude/CLAUDE.md`. Applies to all projects.

## Commits

- Commit when a **meaningful unit of work** is complete.
- Do NOT commit just because of /new or context clearing — only when there is something worth recording.
- Do NOT commit if tests are failing. Fix first.
- Commit messages MUST include:
  - Summary of changes
  - Context for the next session (current state, caveats, next steps)

## Context Clearing (/new)

- Clear when context **approaches saturation** or when switching to a **substantially different task**.
- Do NOT clear mechanically on a fixed schedule. Continue if there is context budget and continuity helps.

## Spec Driven Development (SDD)

All projects use SDD with openspec unless stated otherwise.

- OpenSpec specs are the single source of truth for project requirements.
- Match process to scope:
  - **Large changes** (new capability, cross-cutting): full openspec change (proposal → specs → design → tasks → **verify**)
  - **Medium changes**: openspec change with brief proposal + tasks, specs if needed, skip design → **verify**
  - **Small changes** (bug fix, tweak): no openspec change — edit code, update spec if behavior changed, **run tests**, commit
- Each change: tasks grouped into max 5 with strong logical dependencies.

## Spec Guardrails

- **MUST NOT** delete or weaken existing spec requirements without explicit human approval.
- An implementer MUST NOT modify the spec they are implementing against.
- After archiving a change, review spec diffs for unintended modifications.

## Verification

- Never mark a feature complete without running tests that prove it works.
- Spec scenarios (WHEN/THEN) are acceptance criteria — verify each one.
- Run tests before committing. Do not commit with failing tests.
- **Visual/E2E testing**: For user-facing features, use browser automation (Puppeteer MCP) to screenshot and verify UI. Visual regression catches layout breaks that unit tests miss.

## Session State

- Use `status.md` at project root as the **single source of truth** for current session state.
- Update after every significant action or state change.
- Read `status.md` first on startup to understand: Active Change, Current Task, Status (ready/blocked/error), Blockers.
