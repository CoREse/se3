# se3-config Specification

## Purpose

Define the SE 3.0 framework configuration system via `se3.yaml`. This spec governs configurable parameters, default values, and how the framework adapts its behavior based on project-specific settings.

## Requirements

### Requirement: Configuration File Format

The system SHALL support configuring framework behavior via `se3.yaml`.

The configuration file is located at the project root using YAML format. All configuration items MUST have sensible defaults.

The system SHALL also support a global config at `~/.se3/config.yaml`. Project-level config overrides global config at the top-level key level (no deep merge).

**Configurable options:**
- `version.enabled`: Enable automatic version bumping (default: true)
- `version.file_path`: Path to version file (auto-detect if null)
- `version.bump_rules`: Map task types to bump types
- `version.auto_bump`: Auto-apply version bump without confirmation (default: true)
- `confirmation.enabled`: Enable CONFIRM steps (default: false)
- `confirmation.steps`: Steps after which to insert CONFIRM (default: [plan])
- `confirmation.reviewer`: Who reviews — "human" or "llm" (default: "human")
- `claude_commands`: List of `{cmd, priority}` for Claude CLI resolution
- `language.language`: Language for human-facing steps (default: null)
- `language.spec_language`: Language for spec writing (default: null)
- `issue_discovery.steps`: Steps that receive issue discovery prompt injection (string list, default: ["summarize"])
- `conflict_resolver.strategy`: Merge conflict resolution strategy — `"human"` or `"llm"` (default: `"human"`)
- `implement.group_loc_threshold`: LOC threshold for collapsing task groups into a single LLM call (default: 300)
- `workflow.max_fix_iterations`: Max fix loop iterations before FAILED (default: 20)
- `test.command`: Primary test command override (default: null = auto-detect)
- `test.timeout`: Fallback timeout (seconds) when dynamic timeout is unavailable (default: 1800)
- `test.timeout_multiplier`: Multiplier applied to implement's `estimated_test_duration` to compute the dynamic timeout for the primary test command (default: 2.0, clamped to >= 1.0)
- `test.min_dynamic_timeout`: Lower bound (seconds) on the computed dynamic timeout (default: 30)
- `test.max_dynamic_timeout`: Upper bound (seconds) on the computed dynamic timeout, preventing runaway escalation in the timeout fix loop (default: 14400)
- `test.phases`: Additional test phases (list of phase configs; each phase's own `timeout` is always used and is NOT affected by the dynamic timeout)

#### Scenario: Using default configuration
- **WHEN** no se3.yaml file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom version configuration
- **WHEN** se3.yaml specifies custom bump_rules
- **THEN** the framework uses those rules for version bumping

#### Scenario: Global configuration
- **WHEN** `~/.se3/config.yaml` exists with `claude_commands`
- **THEN** the framework uses the global config as fallback

#### Scenario: Project overrides global
- **WHEN** both global and project configs define the same key
- **THEN** the project-level config takes precedence

### Requirement: Version Configuration

The system SHALL support version management configuration.

**Version section options:**
- `enabled`: Whether automatic version bumping is enabled (default: true)
- `file_path`: Path to version file (null = auto-detect)
- `bump_rules`: Map task type to bump type
  - feature: minor
  - bugfix: patch
  - small: patch
  - review: none
  - directive: minor
- `auto_bump`: Apply bump automatically (default: true)
- `confidence_threshold`: Require confirmation for low confidence (default: null)

#### Scenario: Disable version bumping
- **GIVEN** se3.yaml has `version.enabled: false`
- **WHEN** commit step executes
- **THEN** no version bump is performed

#### Scenario: Custom bump rules
- **GIVEN** se3.yaml defines custom bump_rules
- **WHEN** version_analyze step runs
- **THEN** it uses the custom rules to determine bump type

### Requirement: Confirmation Configuration

The system SHALL support confirmation step configuration.

**Confirmation section options:**
- `enabled`: Whether to insert CONFIRM steps (default: false)
- `steps`: List of steps after which to insert CONFIRM (default: [plan])
- `reviewer`: Who performs the review — "human" or "llm" (default: "human")
- `llm_reviewer.model`: LLM model for review (default: null = default model)
- `llm_reviewer.max_iterations`: Max review-modify cycles (default: 3)

**Human review response mechanism:**

The CONFIRM step supports two response pathways:
1. **Interactive**: When `se3 run` is running, the run loop displays the reviewed step's outputs and prompts the user to approve or request changes directly in the terminal.
2. **File-based**: A call file is created at `se3/calls/confirm_{step_id}_{timestamp}.json`. A response file with `.response` suffix containing `{"approved": bool, "feedback": string}` can be placed alongside it. On resume, the confirm step detects and processes the response file.

The interactive pathway writes the `.response` file automatically, so both pathways converge on the same mechanism.

#### Scenario: Enable confirmation
- **GIVEN** se3.yaml has `confirmation.enabled: true`
- **WHEN** flow reaches a configured step
- **THEN** a CONFIRM step is inserted after it
- **AND** the step pauses and prompts the user for review

#### Scenario: Interactive approval
- **GIVEN** a CONFIRM step is paused
- **WHEN** the run loop detects the pause
- **THEN** it displays the reviewed step's outputs
- **AND** prompts the user to approve, request changes, or exit

#### Scenario: File-based approval
- **GIVEN** a CONFIRM step created a call file
- **WHEN** a `.response` file is placed alongside it
- **AND** user runs `se3 run --resume`
- **THEN** the confirm step reads the response and continues

### Requirement: Conflict Resolver Configuration

The system SHALL support configuring merge conflict resolution strategy for loop branch merges.

**Conflict resolver section options:**
- `conflict_resolver.strategy`: Resolution strategy (default: `"human"`)
  - `"human"`: Preserve conflict state in working tree, create a call file at `se3/calls/merge_conflict_{timestamp}.json` with conflict details, and return `pending_human` to the caller. The user resolves conflicts manually.
  - `"llm"`: Attempt per-file LLM-based conflict resolution. Each conflicting file is sent to the LLM for resolution. If all files are resolved successfully, the merge completes automatically. If any file fails (LLM output still contains conflict markers), falls back to `"human"` mode.

#### Scenario: Default conflict resolution
- **WHEN** no `conflict_resolver` section exists in se3.yaml
- **THEN** the framework uses `"human"` strategy (preserve conflicts, create call file)

#### Scenario: LLM conflict resolution
- **GIVEN** `conflict_resolver.strategy: "llm"` in se3.yaml
- **WHEN** a merge conflict occurs during loop branch merge
- **THEN** the framework attempts to resolve each conflicting file via LLM
- **AND** if all files are resolved, the merge completes automatically
- **AND** if any file fails, falls back to human mode

### Requirement: Implement Configuration

The system SHALL support configuration for the implement step's execution strategy.

**Implement section options:**
- `implement.group_loc_threshold`: Total estimated LOC threshold below which all task groups are collapsed into a single LLM call (default: 300). When the sum of `estimated_loc` across all tasks in all groups is at or below this threshold, the implement step merges all groups into one call instead of executing them as separate DAG-parallel groups.

**Example configuration:**
```yaml
implement:
  group_loc_threshold: 300  # Collapse groups when total LOC ≤ 300
```

#### Scenario: Default implement configuration
- **WHEN** no `implement` section exists in se3.yaml
- **THEN** the framework uses a default `group_loc_threshold` of 300

#### Scenario: Custom LOC threshold
- **GIVEN** `implement.group_loc_threshold: 500` in se3.yaml
- **WHEN** `plan` produces groups with total estimated_loc = 400
- **THEN** the implement step collapses all groups into a single LLM call

### Requirement: Workflow Configuration

The system SHALL support workflow-level configuration for the fix loop mechanism.

**Workflow section options:**
- `workflow.max_fix_iterations`: Maximum number of fix loop iterations before the flow is marked FAILED (default: 20). The fix loop counter is shared across TEST, SELF_CHECK, and VERIFY_SPEC steps. When exhausted, the state machine sets the flow to FAILED status, generates an A-class issue, and stops execution.

**Example configuration:**
```yaml
workflow:
  max_fix_iterations: 20  # Allow up to 20 fix loop iterations
```

#### Scenario: Default workflow configuration
- **WHEN** no `workflow` section exists in se3.yaml
- **THEN** the framework uses a default `max_fix_iterations` of 20

#### Scenario: Custom max fix iterations
- **GIVEN** `workflow.max_fix_iterations: 10` in se3.yaml
- **WHEN** the fix loop reaches 10 iterations without resolving all issues
- **THEN** the state machine sets the flow to FAILED status
- **AND** an A-class issue is generated describing the unresolved problems

### Requirement: Test Configuration

The system SHALL support configuration for the test step's execution, including a dynamic timeout mechanism driven by the implement step's estimate of the test suite runtime.

**Test section options:**
- `command`: Primary test command (default: null = auto-detect)
- `timeout`: Fallback timeout in seconds, used when `estimated_test_duration` from the implement step is missing or invalid (default: 1800)
- `timeout_multiplier`: Multiplier applied to implement's `estimated_test_duration` to derive the actual timeout for the primary test command (default: 2.0). Values below 1.0 are clamped up to 1.0 at load time so a typo cannot silently disable the feature.
- `min_dynamic_timeout`: Lower bound in seconds on the computed dynamic timeout (default: 30)
- `max_dynamic_timeout`: Upper bound in seconds on the computed dynamic timeout (default: 14400). When the user's `test.timeout` is larger than the default ceiling, the framework raises the ceiling to at least `test.timeout` so an explicit high fallback is never silently capped.
- `phases`: List of additional test phases. Each phase's own `timeout` is always used; the dynamic timeout mechanism does NOT apply to phases.
- `fix_loop.max_iterations`: Per-step override for the fix loop iteration budget (defaults to `workflow.max_fix_iterations`)

**Example configuration:**
```yaml
test:
  command: null
  timeout: 1800
  timeout_multiplier: 2.0
  min_dynamic_timeout: 30
  max_dynamic_timeout: 14400
  phases:
    - name: "e2e"
      command: "python -m pytest tests/e2e -v"
      timeout: 600
      required: false
      in_fix_loop: false
```

#### Scenario: Default test configuration
- **WHEN** no `test` section exists in se3.yaml
- **THEN** the framework uses `timeout=1800`, `timeout_multiplier=2.0`, `min_dynamic_timeout=30`, `max_dynamic_timeout=14400`

#### Scenario: Dynamic timeout from implement estimate
- **GIVEN** implement produced `estimated_test_duration: 180`
- **AND** `test.timeout_multiplier: 2.0` in se3.yaml
- **WHEN** the test step runs the primary command
- **THEN** the primary command's timeout is `180 * 2.0 = 360` seconds (within min/max bounds)

#### Scenario: Fallback when implement estimate missing
- **GIVEN** `estimated_test_duration` is missing or non-positive in implement's output
- **WHEN** the test step runs the primary command
- **THEN** the primary command uses `test.timeout` (default 1800 seconds)

#### Scenario: Invalid timeout_multiplier is clamped
- **GIVEN** `test.timeout_multiplier: 0.1` in se3.yaml (or a non-numeric value)
- **WHEN** TestConfig is loaded
- **THEN** the value is normalized (clamped to >= 1.0 or reset to the default 2.0) and a warning is logged

#### Scenario: max_dynamic_timeout respects user's fallback timeout
- **GIVEN** `test.timeout: 20000` in se3.yaml (legitimately slow suite) with no explicit `max_dynamic_timeout`
- **WHEN** TestConfig is loaded
- **THEN** `max_dynamic_timeout` defaults to at least `test.timeout` (20000), not the built-in 14400 ceiling

### Requirement: Language Configuration

The system SHALL support two-tier language configuration for controlling output language.

**Language section options:**
- `language.language`: Language for human-facing steps — summarize, discovery, and steps with human confirmation (default: null = no restriction)
- `language.spec_language`: Language for spec writing in the update_spec step (default: null = no restriction)

When a language is set, a language instruction is appended to the LLM prompt for applicable steps. When null (default), no language instruction is added and the LLM freely chooses language.

**Affected steps by `language.language`:**
- `summarize` — always affected
- `discovery` — always affected
- Steps configured in `confirmation.steps` when `confirmation.enabled: true` and `confirmation.reviewer: "human"` (e.g., `plan`)

**Affected steps by `language.spec_language`:**
- `update_spec` — always affected

**Unaffected steps (LLM decides language):**
- `analyze`, `read_spec`, `plan`, `implement`, `test`, `verify_spec`, `commit`

**Example configuration:**
```yaml
language:
  language: zh-CN        # Human-facing outputs in Chinese
  spec_language: en      # Specs written in English
```

#### Scenario: Default language configuration (null)
- **WHEN** no language section exists in se3.yaml, or both values are null
- **THEN** no language instruction is added to any step
- **AND** the LLM freely chooses language for all outputs

#### Scenario: General language set
- **GIVEN** `language.language: zh-CN` in se3.yaml
- **WHEN** the summarize or discovery step runs
- **THEN** the LLM prompt includes a language instruction to respond in zh-CN

#### Scenario: Spec language set
- **GIVEN** `language.spec_language: en` in se3.yaml
- **WHEN** the update_spec step runs
- **THEN** the LLM prompt includes a language instruction to respond in English

#### Scenario: Confirmed steps use general language
- **GIVEN** `language.language: zh-CN` and `confirmation.enabled: true` with `confirmation.reviewer: "human"` and `confirmation.steps: ["plan"]`
- **WHEN** the plan step runs
- **THEN** the LLM prompt includes a language instruction to respond in zh-CN

#### Scenario: Independent language settings
- **GIVEN** `language.language: zh-CN` and `language.spec_language: en`
- **WHEN** summarize step runs, it uses zh-CN
- **AND** when update_spec step runs, it uses English
- **AND** when implement step runs, no language instruction is added

### Requirement: Claude Commands Configuration

The system SHALL support Claude CLI command resolution.

**Claude commands format:**
```yaml
claude_commands:
  - cmd: "claude"
    priority: 0
  - cmd: "claude-dev"
    priority: 1
```

#### Scenario: Command fallback
- **GIVEN** multiple claude_commands configured
- **WHEN** first command fails or hits rate limit
- **THEN** the framework tries the next command in priority order
