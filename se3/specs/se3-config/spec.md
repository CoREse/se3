# se3-config Specification

## Purpose

Define the SE 3.0 framework configuration system via `se3.yaml`. This spec governs configurable parameters, default values, and how the framework adapts its behavior based on project-specific settings.

## Requirements

### Requirement: Configuration File Format

The system SHALL support configuring framework behavior via `se3.yaml`.

The configuration file is located at the project root using YAML format. All configuration items MUST have sensible defaults.

The system SHALL also support a global config at `~/.se3/config.yaml`. Project-level config overrides global config at the top-level key level (no deep merge).

**Local override file (`se3.local.yaml`):**

The system SHALL support an optional `se3.local.yaml` in the project
root for developer-local overrides.

- When `<project_root>/se3.local.yaml` exists, the framework reads **it
  instead of** `se3.yaml` as the project-level config source. The two
  files are NOT deep-merged — `se3.local.yaml` entirely replaces
  `se3.yaml` for the duration of the load. Developers who want to
  retain values from `se3.yaml` must copy them explicitly into
  `se3.local.yaml`.
- The global + project merge rules (entry-level for `agents` and
  `confirmation.steps`, whole-replace for `llm_caller.defaults` and
  `llm_caller.steps.<step>`) still apply, using `se3.local.yaml` as
  the project source.
- `se3.local.yaml` SHALL be gitignored by default (added by `se3 init`
  to the generated `.gitignore`) so that machine-specific overrides do
  not leak into commits.
- Project-root parent-walk detection (used by `se3 run`, `se3 issue`,
  `se3 history`, `se3 salvage`) SHALL recognise a directory containing
  only `se3.local.yaml` as a valid SE3 project root, for parity with
  `se3.yaml`.
- Warning / deprecation log messages emitted while loading the project
  config SHALL reference the actual filename that was read
  (`se3.local.yaml` when it is present, otherwise `se3.yaml`).

**Configurable options:**
- `version.enabled`: Enable automatic version bumping (default: true)
- `version.file_path`: Path to version file (auto-detect if null)
- `version.bump_rules`: Map task types to bump types
- `version.auto_bump`: Auto-apply version bump without confirmation (default: true)
- `agents`: Top-level dict registry `{name: {type, cmd, priority?}}` (authoritative identity layer; see Agent Registry requirement)
- `claude_commands`: Legacy alias for `agents`, auto-migrated at load time (deprecated — use `agents` + `llm_caller.defaults`)
- `llm_caller.defaults`: Default caller chain as a list of agent names referencing `agents` (default: built-in `[claude]` fallback)
- `llm_caller.steps.<step_name>`: Per-step hard override as a list of agent names (optional)
- `confirmation.steps`: Per-step confirmation dict `{<step_name>: {reviewer?, max_iterations?}}` — steps not listed are NOT confirmed (there is no global `enabled` switch; see Confirmation Configuration requirement)
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
- **WHEN** `~/.se3/config.yaml` defines top-level `agents` entries
- **THEN** the framework uses the global config as fallback

#### Scenario: Project overrides global
- **WHEN** both global and project configs define the same top-level key
- **THEN** the project-level config takes precedence
- **AND** `agents` dict and `confirmation.steps` dict merge entry-level (by name / step_name), while `llm_caller.defaults` and `llm_caller.steps.<step>` are whole-replaced

#### Scenario: se3.local.yaml replaces se3.yaml when present
- **GIVEN** the project root contains both `se3.yaml` and `se3.local.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** only `se3.local.yaml` is consulted as the project source
- **AND** values from `se3.yaml` that are absent from `se3.local.yaml`
  do NOT leak through (no deep merge)

#### Scenario: Only se3.local.yaml exists
- **GIVEN** the project root contains `se3.local.yaml` but no `se3.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** `se3.local.yaml` is used as the project-level config source

#### Scenario: Only se3.yaml exists (no local override)
- **GIVEN** the project root contains `se3.yaml` but no `se3.local.yaml`
- **WHEN** the framework loads project-level configuration
- **THEN** `se3.yaml` is used as the project-level config source
  (existing behavior, unchanged)

#### Scenario: Project detected with only se3.local.yaml
- **GIVEN** a directory contains `se3.local.yaml` but no `se3.yaml`
- **WHEN** any SE3 CLI command (`se3 run`, `se3 issue`, `se3 history`,
  `se3 salvage`) performs parent-walk project-root detection
- **THEN** the directory is recognised as the SE3 project root

#### Scenario: se3.local.yaml is gitignored by se3 init
- **WHEN** `se3 init` generates the project `.gitignore`
- **THEN** the generated `.gitignore` contains an entry that ignores
  `se3.local.yaml` so the local override file is not committed

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

The system SHALL support confirmation (review) step configuration
through a **per-step dict model**. There is no global on/off switch —
the presence of a step in `confirmation.steps` is itself the signal
that the step should be confirmed. Steps not listed are not confirmed.

**Schema:**

```yaml
confirmation:
  steps:                      # dict[step_name, step_config]
    plan:    { reviewer: human }
    design:  { reviewer: reviewer_bot, max_iterations: 3 }
    propose: {}               # reviewer omitted → falls back to llm_caller.defaults
```

Each `step_config` accepts:

- `reviewer`: either
  - `"human"` — route review through the MCP call file / interactive
    pathway (never looked up in the agent registry), or
  - an agent name registered in top-level `agents` — build a
    single-agent `LLMCaller` for that agent and run LLM review, or
  - omitted or `null` — run LLM review using the chain defined by
    `llm_caller.defaults`.
- `max_iterations`: positive integer bound on the review ↔ revise
  cycle. Applied only when `reviewer` resolves to an LLM path (never
  for `reviewer: human`). Omitted values use the built-in default.

**No global toggle:**

The old fields `confirmation.enabled`, the global `confirmation.reviewer`,
the `confirmation.llm_reviewer` subtree (including `llm_reviewer.model`
and `llm_reviewer.max_iterations`), and the list-form
`confirmation.steps: [step, step]` are all **removed**. When present in
a legacy config, each is logged once as a deprecation warning and
ignored — the framework does NOT auto-map them to the new schema.
Temporary global-off workflows are intentionally left to a future CLI
flag / environment variable; they are NOT covered by the config.

**Name resolution and fail-fast:**

A string `reviewer` value other than `"human"` MUST resolve to an entry
in the agent registry. Unknown names produce a startup-time error (see
Agent Registry requirement for the error-message format; the reference
location is `confirmation.steps.<step>.reviewer`).

**Global + project merge:**

`confirmation.steps` is merged **entry-level** by step name: a project
entry for `plan` overrides a global entry for `plan`; a global entry
for `design` without a project counterpart remains in effect.

**Human review response mechanism (unchanged):**

The CONFIRM step for a `reviewer: human` config supports two response
pathways:

1. **Interactive**: When `se3 run` is running, the run loop displays
   the reviewed step's outputs and prompts the user to approve or
   request changes directly in the terminal.
2. **File-based**: A call file is created at
   `se3/calls/confirm_{step_id}_{timestamp}.json`. A response file
   with `.response` suffix containing
   `{"approved": bool, "feedback": string}` can be placed alongside
   it. On resume, the confirm step detects and processes the response
   file.

The interactive pathway writes the `.response` file automatically, so
both pathways converge on the same mechanism.

#### Scenario: Steps not listed are not confirmed
- **GIVEN** `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the flow completes `design`, `implement`, or any step other
  than `plan`
- **THEN** no CONFIRM step is inserted after it

#### Scenario: Listed step triggers CONFIRM insertion
- **GIVEN** `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the flow completes `plan`
- **THEN** a CONFIRM step is inserted after it

#### Scenario: reviewer 'human' uses MCP call pathway
- **GIVEN** a step's config is `{ reviewer: human }`
- **WHEN** the CONFIRM step executes
- **THEN** a call file is created at
  `se3/calls/confirm_{step_id}_{timestamp}.json`
- **AND** the flow pauses awaiting interactive approval or a
  `.response` file

#### Scenario: reviewer = agent name uses single-agent LLM review
- **GIVEN** `agents.reviewer_bot` is registered
- **AND** a step's config is `{ reviewer: reviewer_bot, max_iterations: 3 }`
- **WHEN** the CONFIRM step executes
- **THEN** the framework constructs an `LLMCaller` whose chain is just
  `[reviewer_bot]` and performs LLM review
- **AND** the review ↔ revise loop honours `max_iterations: 3`

#### Scenario: reviewer omitted falls back to llm_caller.defaults
- **GIVEN** `llm_caller.defaults: [primary, backup]`
- **AND** a step's config is `{}` (no `reviewer` field) or
  `{ reviewer: null }`
- **WHEN** the CONFIRM step executes
- **THEN** the framework constructs an `LLMCaller` whose chain is the
  resolved `llm_caller.defaults` list and performs LLM review

#### Scenario: max_iterations ignored for reviewer 'human'
- **GIVEN** a step's config is `{ reviewer: human, max_iterations: 5 }`
- **WHEN** the CONFIRM step executes
- **THEN** `max_iterations` has no effect — the human MCP call pathway
  waits on the user with no iteration cap

#### Scenario: Unknown reviewer name fails fast
- **GIVEN** `confirmation.steps.plan: { reviewer: ghost }` and `ghost`
  is not in the agent registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`confirmation.steps.plan.reviewer`) and lists registered agent names

#### Scenario: Deprecated confirmation.enabled is ignored with warning
- **GIVEN** a legacy config sets `confirmation.enabled: false`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** `enabled` has no effect — step membership in
  `confirmation.steps` alone determines whether CONFIRM is inserted

#### Scenario: Deprecated list-form steps is ignored with warning
- **GIVEN** a legacy config sets `confirmation.steps: [plan, design]`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** no steps are confirmed (the list is not auto-mapped to the
  new dict form)

#### Scenario: Deprecated global reviewer is ignored with warning
- **GIVEN** a legacy config sets top-level `confirmation.reviewer: human`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged
- **AND** the field has no effect — reviewers are read per step from
  `confirmation.steps.<step>.reviewer`

#### Scenario: Deprecated llm_reviewer subtree is ignored with warning
- **GIVEN** a legacy config sets `confirmation.llm_reviewer.model: …`
  and/or `confirmation.llm_reviewer.max_iterations: …`
- **WHEN** the framework loads confirmation config
- **THEN** a deprecation warning is logged for the subtree
- **AND** the fields have no effect — `max_iterations` is configured
  per step and the model is chosen via the agent registry

#### Scenario: Entry-level merge of confirmation.steps
- **GIVEN** global config declares
  `confirmation.steps: { plan: { reviewer: human } }`
- **AND** project config declares
  `confirmation.steps: { design: { reviewer: reviewer_bot } }`
- **THEN** the merged config confirms BOTH `plan` (with `human`, from
  global) AND `design` (with `reviewer_bot`, from project)

#### Scenario: Project overrides global entry for same step
- **GIVEN** global config declares
  `confirmation.steps.plan: { reviewer: human }`
- **AND** project config declares
  `confirmation.steps.plan: { reviewer: reviewer_bot, max_iterations: 2 }`
- **THEN** the merged config confirms `plan` with reviewer
  `reviewer_bot` and `max_iterations: 2`; the global entry is replaced
  entirely

#### Scenario: Interactive approval
- **GIVEN** a CONFIRM step for a `reviewer: human` config is paused
- **WHEN** the run loop detects the pause
- **THEN** it displays the reviewed step's outputs
- **AND** prompts the user to approve, request changes, or exit

#### Scenario: File-based approval
- **GIVEN** a CONFIRM step for a `reviewer: human` config created a
  call file
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
- Steps listed in `confirmation.steps` whose per-step `reviewer` is `"human"` (e.g., `plan`)

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
- **GIVEN** `language.language: zh-CN` and `confirmation.steps: { plan: { reviewer: human } }`
- **WHEN** the plan step runs
- **THEN** the LLM prompt includes a language instruction to respond in zh-CN

#### Scenario: Independent language settings
- **GIVEN** `language.language: zh-CN` and `language.spec_language: en`
- **WHEN** summarize step runs, it uses zh-CN
- **AND** when update_spec step runs, it uses English
- **AND** when implement step runs, no language instruction is added

### Requirement: Agent Registry

The system SHALL maintain a top-level **agent registry** at the `agents`
key of `se3.yaml` (and `~/.se3/config.yaml`). The registry is the sole
identity layer for agents: every other part of the configuration that
needs an agent (`llm_caller.defaults`, `llm_caller.steps.<step>`,
`confirmation.steps.<step>.reviewer`) references it **by name**.

**Registry shape:**

`agents` MUST be a dict whose keys are unique agent names and whose
values are `AgentDef` dicts. Each `AgentDef` has:

- `type`: agent type identifier (default `"claude-code"`)
- `cmd`: CLI command invoked when the agent is selected (required)
- `priority`: integer used to order a chain; higher priority is tried
  first (default `0`)

Name uniqueness is inherent to the dict form: duplicate names in the
same YAML are resolved by YAML itself (last wins). Across global +
project configs, `agents` is merged **entry-level by name**: a project
entry overrides a global entry with the same name; non-conflicting
entries coexist.

**Example:**
```yaml
agents:
  primary:      { type: claude-code, cmd: claude,      priority: 10 }
  backup:       { type: claude-code, cmd: claude-dev,  priority: 5 }
  opus:         { type: claude-code, cmd: claude-opus, priority: 20 }
  reviewer_bot: { type: claude-code, cmd: claude-opus }
```

**Legacy compatibility:**

- **Top-level list form `agents: [...]`** — removed. Detected at load
  time, emits a warning, and is **silently ignored**. The default caller
  chain then falls back to the built-in `[claude]`.
- **`claude_commands: [...]`** — legacy alias. Auto-migrated at load
  time when (and only when) the same source does NOT also set the new
  dict-form `agents`:
  - Each entry's `cmd` is slugified to produce a registry name;
    collisions append `_2`, `_3`, etc.
  - Migrated entries are registered in the top-level `agents` dict.
  - The original entry order is also copied into `llm_caller.defaults`
    so the default chain semantics survive.
  - A `DeprecationWarning` is emitted showing the equivalent new
    config snippet.
  - When the source already sets a dict-form `agents`, `claude_commands`
    is ignored with a warning (no migration).

**Reference integrity (fail-fast):**

Any reference to an agent name from `llm_caller.defaults`,
`llm_caller.steps.<step>`, or `confirmation.steps.<step>.reviewer` that
does not resolve to a registry entry is a startup-time configuration
error. The error message SHALL include the reference location and a
sorted list of registered agent names. The special `reviewer: human`
value is not looked up in the registry.

#### Scenario: Registry loaded from dict form
- **WHEN** `agents` is a dict at the top level
- **THEN** each entry is parsed into an `AgentDef` with the given
  `type`, `cmd`, and `priority`
- **AND** the name is taken from the dict key

#### Scenario: Top-level list form is ignored with warning
- **WHEN** `agents` is a list at the top level
- **THEN** the framework emits a warning
- **AND** the registry is built without those entries
- **AND** the default caller chain falls back to the built-in `[claude]`

#### Scenario: Legacy claude_commands auto-migrates
- **GIVEN** a config sets `claude_commands` but not `agents`
- **WHEN** the framework loads the registry
- **THEN** each `claude_commands` entry is registered in the top-level
  `agents` dict under a name slugified from `cmd` (with `_2`, `_3`
  suffixes on collision)
- **AND** the original order is replicated in `llm_caller.defaults`
- **AND** a `DeprecationWarning` is emitted showing the equivalent
  new-schema YAML

#### Scenario: claude_commands ignored when agents is present
- **GIVEN** a config sets both top-level `agents` (dict) and
  `claude_commands`
- **WHEN** the framework loads the registry
- **THEN** `claude_commands` is ignored
- **AND** a warning is logged indicating the alias was discarded

#### Scenario: Global + project entry-level merge
- **GIVEN** global config declares `agents.primary` and `agents.backup`
- **AND** project config declares `agents.primary` and `agents.opus`
- **THEN** the merged registry contains `primary` (project version),
  `backup` (from global), and `opus` (from project)

#### Scenario: Unknown agent name fails fast
- **GIVEN** any reference location (`llm_caller.defaults`,
  `llm_caller.steps.<step>`, or `confirmation.steps.<step>.reviewer`)
  names an agent absent from the registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message includes the reference location and a
  sorted list of registered agent names

### Requirement: LLM Caller Configuration

The system SHALL drive Claude CLI invocation through the `llm_caller`
section, which references the agent registry by name. `llm_caller`
contains two keys:

- `defaults`: a list of agent names forming the default caller chain
- `steps.<step_name>`: a list of agent names forming a hard per-step
  override chain

Both lists accept **only** registered agent names — anonymous / inline
`{cmd: ...}` entries are rejected. Entries are invoked in the list's
written order (name list is authoritative; the registry's `priority`
field provides the ordering for any internal chain that needs it).

**Default chain (`defaults`):**

- When `llm_caller.defaults` is declared, the framework uses it as the
  default caller chain for any step without a per-step override.
- When absent, the chain falls back to built-in `[claude]` (or to the
  chain produced by legacy `claude_commands` auto-migration).
- Global + project merge: **whole replace** — if project config sets
  `llm_caller.defaults`, it fully replaces the global `defaults`.

**Per-step hard override (`steps.<step>`):**

- A step with a declaration under `llm_caller.steps.<step>` uses
  **only** that list as its chain. The `defaults` chain is NOT
  appended as a fallback — users who want a default tail MUST list
  those agents explicitly in the step's override.
- Agent rotation on infrastructure errors happens strictly within the
  step's override list. When exhausted, the call fails rather than
  silently falling back to `defaults`.
- When `agents` is explicitly passed to the `LLMCaller` constructor
  (e.g. by internal helpers such as the JSON extractor), that argument
  takes the highest priority and bypasses both the per-step override
  and `defaults`.
- Steps with no declaration under `llm_caller.steps` continue to use
  `defaults`.
- Global + project merge: **whole replace per step** — if project
  config sets `llm_caller.steps.<step>`, it fully replaces the global
  declaration for that step; other step overrides remain from global.

**Fail-fast on unknown names:**

Any name in `llm_caller.defaults` or `llm_caller.steps.<step>` that is
absent from the agent registry is a startup-time error (see the Agent
Registry requirement for the error-message format).

**Example configuration:**
```yaml
agents:
  primary: { type: claude-code, cmd: claude,      priority: 10 }
  backup:  { type: claude-code, cmd: claude-dev,  priority: 5 }
  opus:    { type: claude-code, cmd: claude-opus, priority: 20 }

llm_caller:
  defaults: [primary, backup]
  steps:
    # Expensive step — opus first, then primary as tail
    implement: [opus, primary]
    # Cheap step — primary only; backup NOT appended
    summarize: [primary]
```

#### Scenario: Default chain from llm_caller.defaults
- **GIVEN** `llm_caller.defaults: [primary, backup]` and a registry
  containing both names
- **WHEN** `load_agents()` builds the default chain
- **THEN** the returned list contains the agent dicts for `primary`
  followed by `backup`

#### Scenario: No per-step override declared
- **WHEN** `llm_caller.steps` has no entry for the current step
- **THEN** the step uses `llm_caller.defaults` (or the built-in fallback)

#### Scenario: Per-step override used
- **GIVEN** `llm_caller.steps.implement: [opus, primary]`
- **WHEN** the implement step constructs its LLMCaller
- **THEN** the caller uses `[opus, primary]` as its complete chain
- **AND** `llm_caller.defaults` is NOT appended as a fallback

#### Scenario: Exhaustion does not fall back to default chain
- **GIVEN** `llm_caller.steps.analyze: [A, B]`
- **WHEN** both A and B fail with infrastructure errors
- **THEN** the LLM call fails rather than rotating to `defaults`

#### Scenario: Explicit agents argument wins over per-step override
- **GIVEN** `llm_caller.steps.analyze` declares an override list
- **WHEN** LLMCaller is constructed with an explicit `agents=[...]`
- **THEN** the explicit argument is used and both the per-step override
  and `defaults` are bypassed

#### Scenario: Other steps unaffected by a single override
- **GIVEN** `llm_caller.steps.implement` is declared but `plan` is not
- **WHEN** the plan step runs
- **THEN** it uses `llm_caller.defaults`, unaffected by the implement
  override

#### Scenario: Unknown name in defaults fails fast
- **GIVEN** `llm_caller.defaults: [primary, ghost]` and `ghost` is not
  in the agent registry
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`llm_caller.defaults`) and lists registered agent names

#### Scenario: Unknown name in per-step override fails fast
- **GIVEN** `llm_caller.steps.implement: [nonexistent]`
- **THEN** the framework raises a configuration error at startup
- **AND** the error message names the reference location
  (`llm_caller.steps.implement`) and lists registered agent names

#### Scenario: Global defaults replaced by project
- **GIVEN** global config declares `llm_caller.defaults: [g1, g2]`
- **AND** project config declares `llm_caller.defaults: [p1]`
- **WHEN** the framework builds the default chain
- **THEN** the chain is exactly `[p1]` — the global list is not merged

#### Scenario: Project overrides global per-step declaration
- **GIVEN** global config declares `llm_caller.steps.implement: [a, b]`
- **AND** project config declares `llm_caller.steps.implement: [c]`
- **WHEN** the implement step runs in the project
- **THEN** the chain is exactly `[c]` — the global list is not merged
