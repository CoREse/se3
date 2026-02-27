# Adaptive Prompts

**Status**: Backlog (Future)
**Created**: 2026-02-24

## Idea

Dynamically adjust prompts based on past performance and project context.

## Motivation

Static prompts don't account for:
- What has worked well in this codebase
- Current project maturity (greenfield vs legacy)
- User preferences (seen in feedback)
- Seasonal patterns (e.g., more bugs before releases)

## Proposed Features

### Success Pattern Learning

- Track which prompt variations lead to successful outcomes
- A/B test prompt modifications
- Learn from user edits to generated code

### Context-Aware Prompts

- Adjust tone based on project phase
- Include relevant examples from codebase
- Reference similar past changes

### User Preference Learning

- Learn from explicit feedback
- Infer from edit patterns (how much user changes output)
- Preference profiles (verbose vs concise, defensive vs optimistic)

## Technical Approach

### Prompt Templates

Base prompts as templates with slots:

```jinja2
You are helping with a {{project_type}} project.
The team prefers {{code_style}} code.
{% if examples %}
Here are similar recent changes:
{{examples}}
{% endif %}

Task: {{task_description}}
```

### Feedback Loop

1. Generate output
2. Observe outcome (user edits, test results)
3. Score prompt effectiveness
4. Update prompt parameters

## Open Questions

- How to balance learning vs consistency?
- Privacy concerns with learning across users?
- How to explain why a prompt was adjusted?
- Evaluation criteria for prompt effectiveness?

## Related

- `src/se3/engine/steps/` — Step implementations use prompts
