# issue-discovery Specification

## Purpose

Define the automatic issue discovery mechanism for the SE3 flow engine. Issue discovery detects potential problems, concerns, or improvement opportunities during flow execution and persists them as trackable issues via the Issue Management system.

## Requirements

### Requirement: Two-class Discovery Model

The system SHALL support two classes of issue discovery:

- **A-class (System-level trigger):** Deterministic, triggered by the flow engine when specific conditions are met (e.g., fix loop exhaustion).
- **B-class (Prompt injection + collection):** Probabilistic, achieved by injecting issue discovery instructions into LLM prompts for whitelisted steps and extracting reported issues from LLM responses.

#### Scenario: A-class trigger on fix loop exhaustion
- **WHEN** the test→verify_spec→implement fix loop reaches `max_fix_iterations`
- **THEN** `IssueDiscovery.create_from_fix_loop_exhaustion()` creates a `high` priority issue
- **AND** the issue includes fix history, last test output, and fix instructions

#### Scenario: B-class injection into whitelisted step
- **WHEN** a whitelisted step (e.g., `verify_spec`, `summarize`) builds its LLM prompt
- **THEN** the issue discovery prompt fragment is appended to the prompt
- **AND** the LLM may optionally report `discovered_issues` in its response

#### Scenario: B-class collection from LLM response
- **WHEN** a whitelisted step completes and its outputs contain `discovered_issues`
- **THEN** `IssueDiscovery.collect_issues_from_output()` parses, deduplicates, and persists them

### Requirement: Whitelist Configuration

The system SHALL determine which steps receive B-class prompt injection via a configurable whitelist.

**Configuration (`se3.yaml`):**
```yaml
issue_discovery:
  steps:
    - verify_spec
    - summarize
```

**Default whitelist:** `["verify_spec", "summarize"]`

**Forbidden steps (hardcoded):** `{"implement", "test"}` — these steps NEVER receive injection regardless of configuration.

The `get_issue_discovery_injection(step_type, project_root)` function encapsulates all whitelist and forbidden-list logic. All handlers call this function uniformly; non-whitelisted steps receive an empty string with no side effects.

#### Scenario: Default whitelist
- **WHEN** no `issue_discovery.steps` is configured in `se3.yaml`
- **THEN** only `verify_spec` and `summarize` receive injection

#### Scenario: Custom whitelist
- **WHEN** `issue_discovery.steps: ["plan", "summarize"]` is configured
- **THEN** `plan` and `summarize` receive injection
- **AND** `verify_spec` does NOT receive injection (removed from whitelist)

#### Scenario: Forbidden step override
- **WHEN** config includes `implement` in `issue_discovery.steps`
- **THEN** `implement` still does NOT receive injection (forbidden list takes precedence)

### Requirement: Injection Prompt Format

The injection prompt SHALL instruct the LLM to optionally report issues outside the current step's scope.

**Data structure for each discovered issue:**
- `title`: Short descriptive title (required)
- `description`: Details about the issue (required)
- `priority_hint`: One of `"error"`, `"warning"`, or `"info"` (required)

The LLM may include `discovered_issues` as a JSON field in its response (for JSON-mode steps like `verify_spec`) or as a JSON code block in natural language responses (for text-mode steps like `summarize`).

#### Scenario: JSON-mode step (verify_spec)
- **WHEN** verify_spec LLM response includes `"discovered_issues"` field
- **THEN** the field is transparently passed through to `step.outputs["discovered_issues"]`

#### Scenario: Text-mode step (summarize)
- **WHEN** summarize LLM response contains a JSON code block with `discovered_issues`
- **THEN** `_extract_discovered_issues()` parses it and stores in `step.outputs["discovered_issues"]`

### Requirement: Deduplication

The system SHALL deduplicate discovered issues within a single flow execution.

**Algorithm:**
1. Normalize titles: lowercase, strip punctuation, collapse whitespace
2. Check token overlap: if >70% of tokens in the shorter title overlap with the longer title, consider duplicate
3. Exact normalized match is always a duplicate

#### Scenario: Near-duplicate titles
- **GIVEN** an issue titled "Missing error handling in auth module" was already created
- **WHEN** a new issue titled "missing error handling in the auth module" is discovered
- **THEN** the new issue is skipped as a duplicate

### Requirement: Priority Mapping

The system SHALL map `priority_hint` from LLM responses to issue priority levels.

**Mapping rules:**
| Step Type | priority_hint | Issue Priority |
|-----------|--------------|---------------|
| verify_spec | error | high |
| verify_spec | warning | medium |
| verify_spec | info | low |
| summarize | error | high |
| summarize | warning | medium |
| summarize | info | low |
| (default) | (any) | medium |

A-class issues from fix loop exhaustion always have `high` priority.

### Requirement: Issue Tagging

All auto-discovered issues SHALL be tagged with:
- `auto-discovered` — identifies the issue as machine-generated
- `source:{step_type}` — identifies which step discovered it (e.g., `source:verify-spec`, `source:summarize`, `source:fix-loop`)

## Architecture

```
Handler (summarize, verify_spec, ...)
    │
    ├── get_issue_discovery_injection(step_type, project_root)
    │       └── Returns prompt fragment or ""
    │
    ├── LLM call (prompt includes injection)
    │
    └── step.outputs["discovered_issues"] = [...]

State Machine (after step completion)
    │
    └── IssueDiscovery.collect_issues_from_output(flow, step_type, outputs)
            ├── Parse discovered_issues
            ├── Deduplicate
            ├── Map priority
            └── IssueManager.create()
```
