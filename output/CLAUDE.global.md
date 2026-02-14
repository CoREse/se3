# Global Conventions

> Place at `~/.claude/CLAUDE.md`. Applies to all projects.

## Commits

- Commit when a **meaningful unit of work** is complete.
- Do NOT commit just because of /new or context clearing — only when there is something worth recording.
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
  - **Large changes** (new capability, cross-cutting): full openspec change (proposal → specs → design → tasks)
  - **Medium changes**: openspec change with brief proposal + tasks, specs if requirements change, skip design
  - **Small changes** (bug fix, tweak): no openspec change — edit code, update spec if behavior changed, commit
- Each change: tasks grouped into max 5 with strong logical dependencies.

## Documentation

- Project docs in `README.md`. Additional docs in `docs/` if needed.
- README should include: project overview, usage, notable design choices, version info as appropriate.
