# Parallel Execution

**Status**: Backlog (Phase 2)
**Phase**: Parallel Execution
**Created**: 2026-02-27

## Goal

Enable concurrent task execution with proper isolation and merge handling.

## Key Deliverables

- [ ] Task-level parallel execution with worktree isolation
- [ ] Parallel LLM calls with result aggregation
- [ ] Static model role mapping (manager/worker/reviewer)
- [ ] Loop mode with independent branches
- [ ] LLM-assisted merge conflict resolution

## Design Principles

- **Isolation over coordination**: Each task runs in its own worktree
- **Explicit merge points**: User controls when to merge parallel work
- **Failure isolation**: One failing task doesn't block others
- **Resource awareness**: Limit parallel calls based on API quotas

## Related

- `se3/specs/_backlog/recursive-workflows.md` — Hierarchical sub-flows (Phase 3)
- `se3/specs/git-worktree-collab/spec.md` — Worktree collaboration spec
