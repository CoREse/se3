# SE3 Module System Specification

## Purpose

The SE3 Module System defines the architecture for separating framework-level conventions from project-specific configurations in SE 3.0. It establishes clear boundaries between:

1. **Framework layer**: Core SE 3.0 conventions and behaviors
2. **Project layer**: Project-specific customizations and configurations

This separation ensures consistency across SE 3.0 projects while allowing teams to tailor conventions to their specific needs.

## Requirements

### Requirement: Project Configuration File Structure

All SE 3.0 projects SHALL have the following configuration file structure:

```
project/
└── .claude/
    ├── CLAUDE.md    # Project-specific conventions (required)
    └── SE3.md       # Framework conventions (required, managed by se3 tool)
```

#### Scenario: New project initialization
- **WHEN** a new SE 3.0 project is initialized
- **THEN** it SHALL have a CLAUDE.md file with project-specific content

#### Scenario: Detecting redundant content
- **WHEN** CLAUDE.md contains duplicate content from SE3.md
- **THEN** `se3 lint` SHALL warn about redundancy

### Requirement: CLAUDE.md (Project-Specific Conventions)

The project-specific CLAUDE.md file SHALL define project-specific conventions, workflows, and guardrails that supplement or override framework defaults.

- CLAUDE.md SHALL exist in every SE 3.0 project
- CLAUDE.md SHALL be a valid Markdown file
- CLAUDE.md SHALL contain project-specific configuration (cannot be empty)
- CLAUDE.md SHALL NOT duplicate content from SE3.md unnecessarily
- CLAUDE.md MAY override framework conventions with project-specific rules
- CLAUDE.md SHALL be maintained by the project team

#### Scenario: Modifying project conventions
- **WHEN** the project team updates CLAUDE.md
- **THEN** the changes SHALL take effect for all subsequent se3 commands

### Requirement: SE3.md (Framework Conventions)

The SE3.md file SHALL provide the core SE 3.0 framework conventions that apply to all projects.

- SE3.md SHALL exist in every SE 3.0 project
- SE3.md SHALL be a valid Markdown file
- SE3.md SHALL contain the official SE 3.0 framework conventions
- SE3.md SHALL NOT be modified manually by project teams
- SE3.md SHALL be managed exclusively through the se3 tool commands
- SE3.md SHALL include version metadata for checksum validation

#### Scenario: Updating framework conventions
- **WHEN** `se3 update` is run
- **THEN** SE3.md SHALL be updated to the latest version

### Requirement: se3 init Command Behavior

The se3 init command SHALL initialize a new or existing project as an SE 3.0 project.

- se3 init SHALL create the .claude directory if it does not exist
- se3 init SHALL create CLAUDE.md if it does not exist
- se3 init SHALL create SE3.md if it does not exist
- se3 init SHALL populate CLAUDE.md with a project-specific template
- se3 init SHALL populate SE3.md with the latest framework conventions
- se3 init SHALL generate and store a checksum for SE3.md
- se3 init SHALL NOT overwrite existing files unless --force option is used
- se3 init SHALL provide a --force option to overwrite existing files

#### Scenario: Initializing a new project
- **WHEN** se3 init is run on a non-SE3 project
- **THEN** it SHALL create .claude/CLAUDE.md and .claude/SE3.md

#### Scenario: Existing project initialization
- **WHEN** se3 init is run on an existing SE3 project without --force
- **THEN** it SHALL NOT modify existing files

#### Scenario: Forced initialization
- **WHEN** se3 init is run with --force
- **THEN** it SHALL overwrite existing configuration files

### Requirement: se3 update Command Behavior

The `se3 update` command SHALL update the SE 3.0 framework conventions in an existing project.

- `se3 update` SHALL regenerate SE3.md from the installed framework template
- `se3 update` SHALL compare local SE3.md version with latest available
- `se3 update` SHALL update the checksum for the new SE3.md
- `se3 update` SHALL preserve CLAUDE.md (project-specific configuration)
- `se3 update` SHALL display a summary of changes during update

#### Scenario: Updating to latest version
- **WHEN** `se3 update` is run and a newer version exists
- **THEN** it SHALL replace SE3.md with the latest version

#### Scenario: Already on latest version
- **WHEN** `se3 update` is run and already on latest version
- **THEN** it SHALL display "Already on latest version" message

## Verification Tests

To verify the module system functionality:

1. **Initialization Test**:
   ```bash
   mkdir test-project && cd test-project
   se3 init
   ls -la .claude/  # Should contain CLAUDE.md and SE3.md
   ```

2. **Update Test**:
   ```bash
   se3 update  # Should check and update to latest version
   ```
