# Know Your People (知人善任)

**Status**: Backlog (Phase 4+)
**Phase**: Intelligent Scheduling
**Created**: 2026-02-24

## Idea

Track granular capability profiles for different models, going beyond simple "good/bad" scores to understand *what each model is good at*.

## Motivation

Current model selection is often binary (use Claude or not). But different models may excel at different aspects of software development:

- Some models may be better at architecture/design
- Others may excel at implementation details
- Some may produce more testable code
- Others may be better at debugging

Understanding these differences enables "horses for courses" model selection.

## Proposed Features

### Capability Dimensions

Track performance across dimensions like:

1. **Task Type**
   - Architecture/design
   - Implementation
   - Testing
   - Debugging
   - Documentation
   - Refactoring

2. **Code Characteristics**
   - Language preference
   - Framework expertise
   - Code style (concise vs verbose)
   - Error handling thoroughness

3. **Quality Metrics**
   - Test coverage of generated code
   - Bug rate in generated code
   - Documentation completeness
   - Edge case handling

### Data Collection

- Automatically collect metrics from flow executions
- Correlate model choice with outcomes
- User feedback integration (thumbs up/down)
- Pattern analysis across similar tasks

### Usage

```python
# Example: Dynamic model selection based on task
task_type = analyze_task_type(description)
best_model = capability_db.best_for(task_type, language="python")
```

## Open Questions

- How to handle cold-start (new model with no history)?
- Privacy: can we learn across projects?
- How to weight recent vs historical performance?
- How to detect when a model version changes?

## Related

- `se3/specs/_backlog/intelligent-scheduling.md` — Phase 4 overall goals
