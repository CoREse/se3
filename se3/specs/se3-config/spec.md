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
- `confirmation.steps`: Steps after which to insert CONFIRM (default: [propose, design])
- `confirmation.reviewer`: Who reviews — "human" or "llm" (default: "human")
- `claude_commands`: List of `{cmd, priority}` for Claude CLI resolution
- `language.language`: Language for human-facing steps (default: null)
- `language.spec_language`: Language for spec writing (default: null)
- `issue_discovery.steps`: Steps that receive issue discovery prompt injection (string list, default: ["verify_spec", "summarize"])
- `conflict_resolver.strategy`: Merge conflict resolution strategy — `"human"` or `"llm"` (default: `"human"`)

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
- `steps`: List of steps after which to insert CONFIRM (default: [propose, design])
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

### Requirement: Language Configuration

The system SHALL support two-tier language configuration for controlling output language.

**Language section options:**
- `language.language`: Language for human-facing steps — summarize, discovery, and steps with human confirmation (default: null = no restriction)
- `language.spec_language`: Language for spec writing in the update_spec step (default: null = no restriction)

When a language is set, a language instruction is appended to the LLM prompt for applicable steps. When null (default), no language instruction is added and the LLM freely chooses language.

**Affected steps by `language.language`:**
- `summarize` — always affected
- `discovery` — always affected
- Steps configured in `confirmation.steps` when `confirmation.enabled: true` and `confirmation.reviewer: "human"` (e.g., `propose`, `design`)

**Affected steps by `language.spec_language`:**
- `update_spec` — always affected

**Unaffected steps (LLM decides language):**
- `analyze`, `read_spec`, `plan_tasks`, `implement`, `test`, `verify_spec`, `commit`

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
- **GIVEN** `language.language: zh-CN` and `confirmation.enabled: true` with `confirmation.reviewer: "human"` and `confirmation.steps: ["propose", "design"]`
- **WHEN** the propose or design step runs
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
