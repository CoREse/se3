# se3-config Specification

## Purpose
Define the SE 3.0 framework configuration system via `se3.config.yaml`. This spec governs configurable parameters, default values, and how the framework adapts its behavior based on project-specific settings.
## Requirements
### Requirement: Configuration File Format
The system SHALL support configuring framework behavior via `se3.config.yaml`.

The configuration file is located at the project root using YAML format. All configuration items MUST have sensible defaults.

Configurable options include:
- `max_tasks_per_change`: Maximum number of tasks per change (default: 5)
- `human_call.timeout_days`: Default timeout days for human calls (default: 7)
- `session.max_progress_entries`: Maximum session records to retain in progress (default: 20)
- `e2e.baseline_dir`: Directory for storing baseline screenshots
- `e2e.diff_threshold`: Pixel difference threshold for visual regression (0.0 - 1.0)
- `e2e.default_viewport`: Default browser viewport size for screenshots

#### Scenario: Using default configuration
- **WHEN** no se3.config.yaml file exists in the project
- **THEN** the framework runs with built-in default values

#### Scenario: Custom configuration
- **WHEN** se3.config.yaml exists and specifies max_tasks_per_change as 3
- **THEN** the framework limits each change to maximum 3 tasks when creating changes

