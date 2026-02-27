# se3 init Redesign

**Status**: Backlog (Phase 1 remaining)
**Created**: 2026-02-27

## Idea

Redesign `se3 init` to only initialize `openspec/` and `se3.config.yaml` — no more `.claude/` SE3 spec installation.

## Motivation

The current `se3 init` copies SE3 framework specs into `.claude/`, coupling project setup to framework internals. The new design should:

- Only create project-level configuration (`se3.yaml`)
- Only set up the spec directory structure (`se3/specs/`)
- Leave `.claude/` management to the user/framework
