# Intelligent Scheduling

**Status**: Backlog (Phase 4)
**Phase**: Intelligent Scheduling
**Created**: 2026-02-27

## Goal

Dynamic model selection based on capability assessment.

## Key Deliverables

- [ ] Model capability assessment module
- [ ] Execution result scoring and capability profiles
- [ ] Dynamic model selection based on task type
- [ ] "All hands on deck" mode for critical tasks
- [ ] Exploration mode for novel problems
- [ ] Capability persistence and cold-start handling

## Design Principles

- **Evidence-based selection**: Decisions based on past performance
- **Exploration vs exploitation**: Balance known good with trying new
- **Transparency**: User can see why a model was selected
- **Override capability**: User can force specific models

## Related

- `se3/specs/_backlog/know-your-people.md` — Granular capability profiling
- `se3/specs/flow-engine/spec.md` — Flow engine foundation
