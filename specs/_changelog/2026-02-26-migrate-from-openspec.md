# Migrate spec system from OpenSpec to SE3-native

**Date:** 2026-02-26

## Summary

Migrated SE3's spec system from `openspec/specs/` to `specs/`.
SE3 no longer depends on the OpenSpec CLI or its directory conventions.

## Changes

- Copied all 15 specs from `openspec/specs/` to `specs/` (format unchanged)
- Updated SE3 engine and commands to read from `specs/` (with `openspec/specs/` fallback)
- Removed OpenSpec CLI availability checks from `se3 start`
- Change tracking now handled by `specs/_changelog/` (lightweight markdown) instead of `openspec/changes/`
- `openspec/` directory left intact as an independent artifact

## Specs migrated

agent-team, change-verifier, git-worktree-collab, human-as-mcp, output-sync,
requirement-intake, se3-commands, se3-config, se3-module-system, se3-scaffold,
se3-workflows, session-protocol, spec-guardrails, spec-lint, status-diagnostics
