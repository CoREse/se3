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
- **THEN** se3 doctor SHALL warn about redundancy

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

#### Scenario: Detecting manual modifications
- **WHEN** SE3.md is modified manually
- **THEN** se3 doctor SHALL detect the tampering and fail the check

#### Scenario: Updating framework conventions
- **WHEN** se3 upgrade is run
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

### Requirement: se3 upgrade Command Behavior

The se3 upgrade command SHALL upgrade the SE 3.0 framework conventions in an existing project.

- se3 upgrade SHALL check for updates to the SE3.md framework file
- se3 upgrade SHALL compare local SE3.md version with latest available
- se3 upgrade SHALL download and install the latest version if available
- se3 upgrade SHALL update the checksum for the new SE3.md
- se3 upgrade SHALL preserve CLAUDE.md (project-specific configuration)
- se3 upgrade SHALL display a summary of changes during upgrade
- se3 upgrade SHALL handle errors during download/installation gracefully

#### Scenario: Upgrading to latest version
- **WHEN** se3 upgrade is run and a newer version exists
- **THEN** it SHALL replace SE3.md with the latest version

#### Scenario: Already on latest version
- **WHEN** se3 upgrade is run and already on latest version
- **THEN** it SHALL display "Already on latest version" message

#### Scenario: Download failure
- **WHEN** se3 upgrade fails to download
- **THEN** it SHALL display an error message and preserve existing SE3.md

### Requirement: se3 doctor Checksum Validation

The se3 doctor command SHALL verify the integrity of the SE3.md framework file.

- se3 doctor SHALL compute a SHA-256 checksum of the current SE3.md file
- se3 doctor SHALL compare computed checksum with stored checksum
- se3 doctor SHALL detect tampering or corruption of SE3.md
- se3 doctor SHALL display detailed error information if check fails
- se3 doctor SHALL provide instructions for remediation (run se3 upgrade)
- se3 doctor SHALL include checksum validation in the overall health check

#### Scenario: Valid SE3.md check
- **WHEN** SE3.md is unmodified and checksum matches
- **THEN** se3 doctor SHALL pass the checksum validation

#### Scenario: Tampered SE3.md check
- **WHEN** SE3.md is manually modified
- **THEN** se3 doctor SHALL fail and report tampering

#### Scenario: Missing checksum
- **WHEN** checksum file is missing
- **THEN** se3 doctor SHALL fail and suggest running se3 init

### Requirement: Backward Compatibility

The SE3 Module System SHALL support projects with existing single CLAUDE.md files (pre-module system).

- The system SHALL provide automated migration when upgrading from older versions
- The system SHALL maintain compatibility with existing se3 tool commands
- The system SHALL preserve project-specific configurations during migration
- The system SHALL display clear migration instructions

#### Scenario: Migrating pre-module system project
- **WHEN** upgrading a pre-module system project
- **THEN** se3 upgrade SHALL automatically split CLAUDE.md into CLAUDE.md + SE3.md

#### Scenario: Running commands on legacy projects
- **WHEN** se3 commands are run on pre-module system projects
- **THEN** they SHALL continue to function correctly with appropriate warnings

#### Scenario: Migration failure
- **WHEN** a migration fails
- **THEN** the tool SHALL provide rollback instructions

## Verification Tests

To verify the module system functionality:

1. **Initialization Test**:
   ```bash
   mkdir test-project && cd test-project
   se3 init
   ls -la .claude/  # Should contain CLAUDE.md and SE3.md
   ```

2. **Checksum Validation Test**:
   ```bash
   se3 doctor  # Should pass
   echo "tampered" >> .claude/SE3.md
   se3 doctor  # Should fail with tampering error
   ```

3. **Upgrade Test**:
   ```bash
   se3 upgrade  # Should check and update to latest version
   ```

4. **Migration Test**:
   ```bash
   # Create a pre-module system project
   mkdir legacy-project && cd legacy-project
   mkdir .claude && echo "legacy content" > .claude/CLAUDE.md
   se3 upgrade  # Should split into CLAUDE.md (legacy content) and SE3.md (framework)
   ```
