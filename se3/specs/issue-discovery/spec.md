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
- **WHEN** the validate→implement fix loop reaches `max_fix_iterations` (default 100). The fix loop is entered when any of the validation step types — `TEST`, `SELF_CHECK`, or `VERIFY_SPEC` — completes with `REVISION_NEEDED` status, so exhaustion can be triggered by repeated revision-needed outcomes from any of these three step types (not only `TEST` and `VERIFY_SPEC`).
- **THEN** `IssueDiscovery.create_from_fix_loop_exhaustion()` creates a `high` priority issue
- **AND** the issue includes fix history (last 5 entries), last test output (tail 1000 chars), and fix instructions (first 1500 chars)
- **AND** the issue description also includes the flow's `flow_id`, the history path (`se3/history/{flow_id}`), and — when a DISCOVERY step with status `COMPLETED` or `PARTIAL` produced a `refined_description` that differs from the original `task_description` — a `**Refined description:**` section containing the refined text (truncated to 1500 chars). A `PARTIAL` DISCOVERY step is treated as a valid source of `refined_description` because partial discovery may still yield a usable refined description; the helper `_effective_task_description_base` in `src/se3/engine/state_machine.py` accepts both `StepStatus.COMPLETED` and `StepStatus.PARTIAL`.
- **AND** the issue description also includes a `**Trigger step:**` line naming the `step_type` value of the validation step that triggered the exhaustion (one of `TEST`, `SELF_CHECK`, or `VERIFY_SPEC`), and a `**Fix iterations:**` line reporting the current fix iteration count obtained via `flow.state.get_fix_iteration()`
- **AND** the flow is set to FAILED status and execution stops (the flow does NOT continue to the next step)
- **NOTE** when `max_fix_iterations == 0` (the user-configured `0`/`null` sentinel for "unlimited"), this A-class trigger never fires because the state machine skips the exhaustion check entirely. Negative values are rejected at config load, so they cannot reach this code path.

#### Scenario: A-class trigger on inherited (baseline) test failures
- **WHEN** the test step detects failures that are **inherited** — present in the frozen pre-implement `baseline_failures` set (captured before this flow's `implement` touched anything), not introduced by the current change (see flow-engine *Pre-implement Test Baseline*). The baseline replaces the retired `se3/state/known_test_failures.json` known-list as the provenance source.
- **THEN** `IssueDiscovery.create_from_pre_existing_failures()` creates a `medium` priority issue
- **AND** the issue lists each inherited failing test with its test_id and reason
- **AND** the issue is tagged with `auto-discovered` and `source:test-pre-existing`
- **AND** the issue is filed **at most once per flow execution** (deduped via `context["inherited_failures_filed"]`) — it is never re-filed on each fix iteration, preventing the historical duplicate-issue explosion

#### Scenario: B-class injection into whitelisted step
- **WHEN** a configured whitelisted step builds its LLM prompt (note: the default whitelist is empty, and `summarize` is no longer a participant)
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
  steps: []          # default: empty — no step receives B-class injection
```

**Default whitelist:** `[]` (empty). The default whitelist constant `ISSUE_DISCOVERY_DEFAULT_STEPS` in `context_builder.py` is the empty list, so out of the box NO step receives B-class prompt injection.

**Note — `summarize` no longer participates in B-class discovery:** The default whitelist previously contained `["summarize"]`. `summarize` has since been reworked into a pure, user-facing session report (see the flow-engine *Summarize Session Report and Completion Gate* requirement); both its injection call AND its `discovered_issues` extraction (`_extract_discovered_issues`) were removed. Re-adding `summarize` to `issue_discovery.steps` is therefore explicitly **unsupported**: even when configured, `summarize` neither appends the injection fragment nor writes `discovered_issues`, so nothing is collected. The whitelist mechanism itself is retained (default empty) so that other steps which can capture `discovered_issues` from their own output may still be opted in explicitly.

**Note:** `verify_spec` is likewise not in the default whitelist. The `verify_spec` step uses a deterministic scope mechanism, but out-of-scope issues are now **logged (留痕) via `_log_out_of_scope_issues`, not filed** via `IssueManager.create()` — see the flow-engine *verify_spec Unified Priority and Scope Mechanism* requirement. This avoids the issue-tracker explosion a looping scoped flow would otherwise cause by re-filing the same out-of-scope observations every iteration.

**Forbidden steps (hardcoded):** `{"implement", "test"}` — these steps NEVER receive injection regardless of configuration.

The `get_issue_discovery_injection(step_type, project_root)` function encapsulates all whitelist and forbidden-list logic. All handlers call this function uniformly; non-whitelisted steps receive an empty string with no side effects.

#### Scenario: Default whitelist is empty
- **WHEN** no `issue_discovery.steps` is configured in `se3.yaml`
- **THEN** no step receives B-class injection (the default whitelist is empty)
- **AND** in particular `summarize` does NOT receive injection

#### Scenario: summarize opt-in is unsupported
- **WHEN** `issue_discovery.steps: ["summarize"]` is configured to try to re-enable injection for summarize
- **THEN** `summarize` still does not append the injection fragment and never produces `discovered_issues`
- **AND** no B-class issues are collected from summarize, because its injection and extraction were removed when it became a pure session report

#### Scenario: Custom whitelist for a capable step
- **WHEN** `issue_discovery.steps: ["plan"]` is configured
- **THEN** `plan` receives injection
- **AND** `verify_spec` does NOT receive injection (it classifies scope deterministically and logs out-of-scope issues instead of filing them)

#### Scenario: Forbidden step override
- **WHEN** config includes `implement` in `issue_discovery.steps`
- **THEN** `implement` still does NOT receive injection (forbidden list takes precedence)

### Requirement: Injection Prompt Format

The injection prompt SHALL instruct the LLM to optionally report issues outside the current step's scope.

**Data structure for each discovered issue:**
- `title`: Short descriptive title (required)
- `description`: Details about the issue (required)
- `priority_hint`: One of `"critical"`, `"high"`, `"medium"`, or `"low"` (required)

The LLM may include `discovered_issues` as a JSON field in its response (for JSON-mode steps) or as a JSON code block in natural language responses (for text-mode steps). **Note:** `summarize` was the original text-mode example, but it no longer participates in B-class discovery — its injection and `discovered_issues` extraction (`_extract_discovered_issues`) were removed when it became a pure session report (see *Whitelist Configuration*).

#### Scenario: JSON-mode step (verify_spec)
- **WHEN** verify_spec LLM response includes `"discovered_issues"` field
- **THEN** the field is transparently passed through to `step.outputs["discovered_issues"]`

#### Scenario: Text-mode step extraction (summarize removed)
- **WHEN** a `summarize` LLM response contains a JSON code block with `discovered_issues`
- **THEN** nothing is extracted — `summarize` no longer has a `_extract_discovered_issues()` path and never writes `step.outputs["discovered_issues"]`
- **AND** the text-mode JSON-code-block extraction format remains available only for any future text-mode step that is explicitly given such a collector

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

### Requirement: Auto-discovered Issue Type

All auto-discovered issues (both A-class and B-class) SHALL be created with `type="bug"`, regardless of the underlying trigger or the LLM's reported content.

This applies uniformly to:
- A-class fix loop exhaustion issues (`create_from_fix_loop_exhaustion`)
- A-class pre-existing test failure issues (`create_from_pre_existing_failures`)
- B-class collected issues parsed from a step's `discovered_issues` output (`collect_issues_from_output`)

The discovered-issues data structure does NOT include a user-controllable type field; the type is fixed at the call site to `IssueManager.create()`.

#### Scenario: A-class issue type
- **WHEN** any A-class trigger (`create_from_fix_loop_exhaustion` or `create_from_pre_existing_failures`) creates an issue
- **THEN** the created issue has `type="bug"`

#### Scenario: B-class issue type
- **WHEN** `collect_issues_from_output` creates an issue from a step's `discovered_issues` output
- **THEN** the created issue has `type="bug"`, regardless of any type-like field the LLM may have included in its response

### Requirement: Static Injection Helper API

In addition to the unified `get_issue_discovery_injection(step_type, project_root)` entry point in `context_builder`, the system SHALL expose a parallel static helper `IssueDiscovery.get_injection_prompt(step_type)` on the `IssueDiscovery` class itself.

This static helper provides a config-free lookup against the hardcoded `ISSUE_DISCOVERY_STEPS` set (currently `{"summarize"}`) defined in `src/se3/engine/issue_discovery.py`. It is intended for callers that do not have a `project_root` available or that need a deterministic answer based solely on the hardcoded whitelist (e.g., internal collection paths that must agree with the same hardcoded set used by `collect_issues_from_output`).

**Signature:**
```python
@staticmethod
def get_injection_prompt(step_type: str) -> Optional[str]
```

**Behavior:**
- Returns the `ISSUE_DISCOVERY_PROMPT` fragment when `step_type` is in the hardcoded `ISSUE_DISCOVERY_STEPS` set.
- Returns `None` for any other step type.
- Does NOT consult `se3.yaml` configuration; it reflects only the hardcoded built-in whitelist.
- Does NOT apply the forbidden-list check (the hardcoded whitelist already excludes forbidden steps by construction).

**Relationship to `get_issue_discovery_injection`:**
- `get_issue_discovery_injection(step_type, project_root)` is the primary entry point used by step handlers; it honors user configuration (including custom whitelists) and the forbidden list, and returns an empty string for non-whitelisted steps.
- `IssueDiscovery.get_injection_prompt(step_type)` is a secondary, parallel API anchored to the hardcoded built-in set; it returns `None` (not `""`) for non-whitelisted steps.

#### Scenario: Hardcoded whitelist hit
- **WHEN** `IssueDiscovery.get_injection_prompt("summarize")` is called
- **THEN** the `ISSUE_DISCOVERY_PROMPT` fragment is returned

#### Scenario: Non-whitelisted step
- **WHEN** `IssueDiscovery.get_injection_prompt("plan")` is called
- **THEN** `None` is returned, regardless of any `issue_discovery.steps` configuration in `se3.yaml`

#### Scenario: Consistency with collection
- **GIVEN** `IssueDiscovery.collect_issues_from_output()` only processes outputs from steps in `ISSUE_DISCOVERY_STEPS`
- **THEN** `IssueDiscovery.get_injection_prompt(step_type)` returns a non-None prompt for exactly the same set of step types

### Requirement: Source Tag Mapping Table

The B-class collection path SHALL derive a step-type-specific `source:*` tag for each collected issue from an internal `_SOURCE_TAG_MAP` lookup in `src/se3/engine/issue_discovery.py`, falling back to `source:{step_type}` for any step type not explicitly present in the map.

**Map contents:**
- `"summarize"` → `"source:summarize"`
- `"verify_spec"` → `"source:verify-spec"`

**Fallback rule:**
- For any `step_type` not present in `_SOURCE_TAG_MAP`, the tag is computed as `f"source:{step_type}"` (i.e., the raw step type string is appended to the `source:` prefix without underscore-to-hyphen translation).

The map exists to give specific step types a non-default tag spelling (e.g., the hyphenated `source:verify-spec` form rather than the underscore-preserving fallback `source:verify_spec`). Step types whose desired tag matches the fallback do not need to appear in the map.

**Note on the `verify_spec` entry:** The `"verify_spec"` mapping is currently unreachable at runtime because `collect_issues_from_output` early-returns for any `step_type` not in `ISSUE_DISCOVERY_STEPS` (which contains only `"summarize"`), so `_SOURCE_TAG_MAP.get(step_type, ...)` is never called with `"verify_spec"`. The entry is retained as a forward-compatible mapping in case `verify_spec` (or another step that previously emitted B-class issues) is re-added to the discovery whitelist; if/when that happens, the existing tag spelling `source:verify-spec` is automatically honored without further code changes.

#### Scenario: Mapped step type uses explicit tag spelling
- **GIVEN** `step_type == "summarize"` and a discovered issue is being collected
- **THEN** the resulting issue is tagged with `source:summarize` (from the map), not the fallback-computed value

#### Scenario: Unmapped step type uses fallback
- **GIVEN** a (hypothetical) whitelisted `step_type == "plan"` with no entry in `_SOURCE_TAG_MAP`
- **THEN** the resulting issue is tagged with `source:plan` (computed via the `f"source:{step_type}"` fallback)

#### Scenario: Dead `verify_spec` entry is harmless
- **GIVEN** `_SOURCE_TAG_MAP` contains a `"verify_spec"` entry but `verify_spec` is not in `ISSUE_DISCOVERY_STEPS`
- **WHEN** a `verify_spec` step completes with `discovered_issues` in its outputs
- **THEN** `collect_issues_from_output` returns an empty list without consulting `_SOURCE_TAG_MAP`, so the `verify_spec` entry has no observable effect

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

verify_spec Deterministic Scope Classification (separate from B-class):

verify_spec handler
    │
    ├── LLM classifies issues with priority + scope
    │
    ├── in_scope issues → trigger REVISION_NEEDED
    │
    └── out_of_scope issues → _log_out_of_scope_issues() (留痕, logged not filed)
```
