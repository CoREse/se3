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
