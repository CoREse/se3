# Autonomous Collaboration

**Status**: Backlog (Phase 3)
**Phase**: Autonomous Collaboration
**Created**: 2026-02-27

## Goal

Enable recursive problem decomposition and multi-manager coordination.

## Key Deliverables

- [ ] Recursive workflow (Problem → Sub-problems → Solutions)
- [ ] Multi-manager coordination with independent contexts
- [ ] Recursive depth limiting and cycle detection
- [ ] Problem-level parallel branch management
- [ ] Cross-problem dependency tracking

## Design Principles

- **Hierarchical organization**: Clear parent/child relationships
- **Context boundaries**: Each sub-problem has isolated context
- **Escalation paths**: Sub-problems can escalate to parent
- **Termination guarantees**: Prevent infinite recursion

## Related

- `se3/specs/_backlog/recursive-workflows.md` — Concrete recursive workflow mechanisms
- `se3/specs/_backlog/parallel-execution.md` — Phase 2 parallel foundation
