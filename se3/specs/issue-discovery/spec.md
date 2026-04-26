<!-- spec-format: v1 -->
# issue-discovery Specification

## Purpose

Define the automatic issue discovery mechanism for the SE3 flow engine. Issue discovery detects potential problems, concerns, or improvement opportunities during flow execution and persists them as trackable issues via the Issue Management system.

## Requirements

### Requirement: Two-class Discovery Model

The system SHALL support two classes of issue discovery:

- **A-class (System-level trigger):** Deterministic, triggered by the flow engine when specific conditions are met (e.g., fix loop exhaustion).
- **B-class (Prompt injection + collection):** Probabilistic, achieved by injecting issue discovery instructions into LLM prompts for whitelisted steps and extracting reported issues from LLM responses.

#### Scenario: A-class trigger on fix loop exhaustion
- **WHEN** the test→verify_spec→implement fix loop reaches `max_fix_iterations` (default 20)
- **THEN** `IssueDiscovery.create_from_fix_loop_exhaustion()` creates a `high` priority issue
- **AND** the issue includes fix history (last 5 entries), last test output (tail 1000 chars), and fix instructions (first 1500 chars)
- **AND** the flow is set to FAILED status and execution stops (the flow does NOT continue to the next step)

#### Scenario: A-class trigger on pre-existing test failures
- **WHEN** the test step detects failures that exist in `se3/state/known_test_failures.json` (not introduced by the current change)
- **THEN** `IssueDiscovery.create_from_pre_existing_failures()` creates a `medium` priority issue
- **AND** the issue lists each pre-existing failing test with its test_id and reason
- **AND** the issue is tagged with `auto-discovered` and `source:test-pre-existing`
- **AND** duplicate issues are suppressed within the same flow execution

#### Scenario: A-class trigger on sync gap detection
- **WHEN** `se3 sync` detects that a spec describes a requirement not implemented in code (gap)
- **THEN** the sync engine creates a `medium` priority issue via `IssueManager.create()`
- **AND** the issue title follows the format `[sync] {spec_name}: {description}`
- **AND** the issue is tagged with `auto-discovered` and `source:sync`
- **AND** idempotency uses normalized matching: titles are normalized by lowercasing, removing articles (a/an/the), stripping punctuation, and collapsing whitespace before comparison
- **AND** if a normalized-matching open issue already exists, creation is skipped (idempotency)
- **AND** `find_open_by_title` uses exact case-insensitive matching (not substring matching)

#### Scenario: A-class trigger on sync gap resolution (auto-close)
- **WHEN** `se3 sync` detects that a previously reported gap is no longer present in the analysis
- **THEN** the sync engine automatically closes the corresponding sync-tagged issue via `IssueManager.close_issue()`
- **AND** the close reason indicates the gap was resolved
- **AND** a three-layer matching strategy prevents false closures:
  1. Normalized match against current gap titles
  2. Prefix fallback: if the issue's spec still has gaps, the issue is kept open
  3. Close only when neither condition holds
- **AND** only gap issues are processed (conflict-tagged issues are excluded)
- **AND** `close_issue` raises `OSError` if the file move fails (rather than silently continuing)

#### Scenario: B-class injection into whitelisted step
- **WHEN** a whitelisted step (e.g., `summarize`) builds its LLM prompt
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
    - summarize
```

**Default whitelist:** `["summarize"]`

**Note:** `verify_spec` was removed from the default whitelist. The `verify_spec` step now uses a deterministic scope mechanism to file out-of-scope issues directly via `IssueManager.create()`, replacing the probabilistic B-class discovery approach.

**Forbidden steps (hardcoded):** `{"implement", "test"}` — these steps NEVER receive injection regardless of configuration.

The `get_issue_discovery_injection(step_type, project_root)` function encapsulates all whitelist and forbidden-list logic. All handlers call this function uniformly; non-whitelisted steps receive an empty string with no side effects.

#### Scenario: Default whitelist
- **WHEN** no `issue_discovery.steps` is configured in `se3.yaml`
- **THEN** only `summarize` receives injection

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
- `priority_hint`: One of `"critical"`, `"high"`, `"medium"`, or `"low"` (required)

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

The system SHALL use `priority_hint` from LLM responses directly as the issue priority level, with no translation mapping.

**Direct mapping rules:**
- `priority_hint` values (`critical`, `high`, `medium`, `low`) are used directly as the issue priority
- If `priority_hint` is not one of the valid values, it defaults to `medium`
- No per-step priority translation table is needed

A-class issues from fix loop exhaustion always have `high` priority.

### Requirement: Issue Tagging

All auto-discovered issues SHALL be tagged with:
- `auto-discovered` — identifies the issue as machine-generated
- `source:{step_type}` — identifies which step discovered it (e.g., `source:verify-spec`, `source:summarize`, `source:fix-loop`)

## Architecture

```
B-class Discovery (summarize, and other whitelisted steps):

Handler (summarize, ...)
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
            ├── Use priority_hint directly as issue priority
            └── IssueManager.create()

verify_spec Deterministic Filing (separate from B-class):

verify_spec handler
    │
    ├── LLM classifies issues with priority + scope
    │
    ├── in_scope issues → trigger REVISION_NEEDED
    │
    └── out_of_scope issues → IssueManager.create() directly
```
