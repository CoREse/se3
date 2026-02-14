# Global Conventions

> Place this file at `~/.claude/CLAUDE.md`. These conventions apply to ALL projects.

## Case Sensitivity

All file names and paths in this document and project-level CLAUDE.md files are CASE SENSITIVE.

## Commits

- Commit after completing a unit of work (before each context clear with /new).
- Commit messages MUST include:
  - Summary of what changed
  - Context useful for the next Claude Code session (current state, caveats, next steps)

## Spec Driven Development (SDD)

All projects use SDD with openspec unless stated otherwise.

- Changes that affect project specs MUST go through openspec changes.
- Each openspec change: tasks grouped into max 5 with strong logical dependencies.
- When applying a change: clear context after each task group, then proceed to the next.
- After applying: verify implementation against spec, archive the change, then commit.

## Documentation

- Project docs go in `README.md`.
- Additional docs in `docs/` directory if needed.
- README should include: project overview, usage guide, notable design choices, version info as appropriate.

## Key Files

- `demands.md`: Project requirements. Managed by AI and human together. Only additive — remove entries only if they conflict with current project direction.
- `progress.md`: Cross-session progress log. AI-managed, reverse chronological.
