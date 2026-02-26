# se3-config Specification

## Purpose
Define the SE 3.0 framework configuration system via `se3.yaml` (with legacy fallback to `se3.config.yaml`). This spec governs configurable parameters, default values, and how the framework adapts its behavior based on project-specific settings.
## Requirements
### Requirement: Configuration File Format
The system SHALL support configuring framework behavior via `se3.yaml`, with automatic fallback to `se3.config.yaml` for backward compatibility.

The configuration file is located at the project root using YAML format. All configuration items MUST have sensible defaults.

The system SHALL also support a global config at `~/.se3/config.yaml`. Project-level config overrides global config at the top-level key level (no deep merge).

Configurable options include:
- `max_tasks_per_change`: Maximum number of tasks per change (default: 5)
- `human_call.timeout_days`: Default timeout days for human calls (default: 7)
- `human_call.language`: Language for human call messages (e.g., `zh-CN`, `en-US`)
- `session.max_progress_entries`: Maximum session records to retain in progress (default: 20)
- `commit.test_command`: Custom test command for `se3 commit` (default: auto-detect pytest)
- `claude_commands`: List of `{cmd, priority}` entries for Claude command resolution with priority-based fallback (default: `[{cmd: "claude", priority: 0}]`)

#### Scenario: Using default configuration
- **WHEN** no se3.yaml (or legacy se3.config.yaml) file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom configuration
- **WHEN** se3.yaml exists and specifies max_tasks_per_change as 3
- **THEN** the framework limits each change to maximum 3 tasks when creating changes

#### Scenario: Global configuration
- **WHEN** `~/.se3/config.yaml` exists with `claude_commands` and no project-level config exists
- **THEN** the framework uses the global `claude_commands` list

#### Scenario: Project overrides global
- **WHEN** both global and project configs define `claude_commands`
- **THEN** the project-level `claude_commands` replaces the global list entirely
