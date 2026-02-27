# Recursive Workflows

**Status**: Backlog (Phase 3)
**Phase**: Autonomous Collaboration
**Created**: 2026-02-24

## Idea

Enable a flow to spawn sub-flows for sub-problems, creating a hierarchical tree of related work.

## Motivation

Complex problems often decompose naturally:

```
Implement User Authentication
├── Design Auth Schema
├── Implement Password Hashing
├── Create Login Endpoint
│   ├── Input Validation
│   ├── Rate Limiting
│   └── Session Management
└── Create Logout Endpoint
```

Current linear flows don't naturally express this hierarchy.

## Proposed Features

### Problem Decomposition

- LLM analyzes task and suggests sub-problems
- User approves/refines decomposition
- Each sub-problem becomes a child flow

### Parent-Child Relationships

- Parent flow waits for children or continues independently
- Children inherit context from parent but can diverge
- Results propagate back to parent

### Resource Management

- Depth limits to prevent infinite recursion
- Budget tracking across hierarchy
- Parallel execution of independent children

### Example Flow

```python
# Parent flow
analysis = analyze_step("Build e-commerce checkout")
if analysis.suggests_decomposition:
    children = decompose_into_children(analysis.sub_problems)
    results = execute_in_parallel(children)
    integrate_results(results)
```

## Technical Considerations

### Context Management

- Parent context available to children (read-only)
- Child context isolated from siblings
- Merge strategy when child completes

### State Persistence

- Hierarchical state files: `flow_{parent}_{child}.json`
- Or flat with parent references

### UI/UX

- Visualize hierarchy in status/dashboard
- Allow drilling into child flows
- Surface blocked children

## Open Questions

- When should decomposition happen automatically vs user approval?
- How to handle partial failures in children?
- What's the right depth limit?
- How to merge conflicting child outputs?

## Related

- `se3/specs/_backlog/autonomous-collaboration.md` — Phase 3 overall goals
- `se3/specs/_backlog/parallel-execution.md` — Phase 2 parallel foundation
- `se3/specs/flow-engine/spec.md` — Flow engine foundation
