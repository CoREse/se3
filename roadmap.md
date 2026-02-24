# SE3 Framework Roadmap

This document outlines the long-term vision and phased development plan for the SE3 framework.

## Overview

SE3 is a session management and workflow framework designed to bring structure and reliability to AI-assisted software development. The current focus is on establishing a solid foundation (Phase 1) before moving to advanced features like parallel execution and autonomous collaboration.

---

## Phase 1: Core Flow Engine (Current)

**Goal**: Establish a reliable, state-machine-driven workflow engine as the foundation.

**Status**: In Progress

### Key Deliverables

- [x] State machine core with JSON persistence
- [x] Step handlers for all workflow phases (analyze, design, implement, test, etc.)
- [x] `se3 run` as the unified entry point
- [x] Interrupt/resume capability
- [x] Structured logging
- [x] Spec indexing and auto-matching
- [x] Deprecation warnings for 2.x commands
- [x] Comprehensive test coverage (unit, integration, e2e)
- [ ] Output template updates for 3.0 architecture
- [ ] `se3 init` redesign: only initialize openspec/ and se3.config.yaml (no more .claude/ SE3 spec installation)

### Design Principles

- **Program-controlled flow**: Step transitions are deterministic, not LLM-driven
- **State persistence**: JSON-based state allows precise interrupt/resume
- **Minimal complexity**: No general workflow engine, just what SE3 needs
- **Backward compatibility**: 2.x commands remain functional as fallback

---

## Phase 2: Parallel Execution

**Goal**: Enable concurrent task execution with proper isolation and merge handling.

**Status**: Planned

### Key Deliverables

- [ ] Task-level parallel execution with worktree isolation
- [ ] Parallel LLM calls with result aggregation
- [ ] Static model role mapping (manager/worker/reviewer)
- [ ] Loop mode with independent branches
- [ ] LLM-assisted merge conflict resolution

### Design Principles

- **Isolation over coordination**: Each task runs in its own worktree
- **Explicit merge points**: User controls when to merge parallel work
- **Failure isolation**: One failing task doesn't block others
- **Resource awareness**: Limit parallel calls based on API quotas

---

## Phase 3: Autonomous Collaboration

**Goal**: Enable recursive problem decomposition and multi-manager coordination.

**Status**: Planned

### Key Deliverables

- [ ] Recursive workflow (Problem → Sub-problems → Solutions)
- [ ] Multi-manager coordination with independent contexts
- [ ] Recursive depth limiting and cycle detection
- [ ] Problem-level parallel branch management
- [ ] Cross-problem dependency tracking

### Design Principles

- **Hierarchical organization**: Clear parent/child relationships
- **Context boundaries**: Each sub-problem has isolated context
- **Escalation paths**: Sub-problems can escalate to parent
- **Termination guarantees**: Prevent infinite recursion

---

## Phase 4: Intelligent Scheduling

**Goal**: Dynamic model selection based on capability assessment.

**Status**: Future

### Key Deliverables

- [ ] Model capability assessment module
- [ ] Execution result scoring and capability profiles
- [ ] Dynamic model selection based on task type
- [ ] "All hands on deck" mode for critical tasks
- [ ] Exploration mode for novel problems
- [ ] Capability persistence and cold-start handling

### Design Principles

- **Evidence-based selection**: Decisions based on past performance
- **Exploration vs exploitation**: Balance known good with trying new
- **Transparency**: User can see why a model was selected
- **Override capability**: User can force specific models

---

## Future Ideas (Backlog)

Ideas that don't fit into current phases but are worth recording:

### "知人善任" (Know Your People)

Track which models excel at which types of tasks, not just generic performance scores. Include:
- Task type affinity (design vs implementation vs testing)
- Code style preferences
- Language/framework expertise
- Error pattern analysis

See: `openspec/backlog/know-your-people.md`

### Adaptive Prompts

Dynamically adjust prompts based on:
- Past successful patterns for this codebase
- Current project phase (greenfield vs maintenance)
- User feedback on previous outputs

### Cross-Project Learning

Share learnings across projects (with privacy considerations):
- Common mistake patterns
- Successful refactoring patterns
- Spec anti-patterns to avoid

---

## Contributing to the Roadmap

1. **Phase 1 issues**: Report bugs or usability issues with the flow engine
2. **Phase 2+ discussion**: Open an issue with the `roadmap` label
3. **Backlog entries**: Add to `openspec/backlog/` with context and rationale

---

## Changelog

| Date | Change |
|------|--------|
| 2026-02-24 | Initial roadmap with Phase 1-4 structure |
