# se3-commands Specification

## Purpose

Define the command-line interface for SE3 core commands. SE3 3.0 uses `se3 run` as the unified entry point for all development workflows, with supporting commands for project initialization and spec protection.

## Requirements

### Requirement: Unified Entry Point `se3 run`

The system SHALL provide `se3 run` as the primary entry point for all SE3 workflows.

**Interface:**
```bash
# New task
se3 run "Implement feature X"

# Resume interrupted flow
se3 run --resume

# Loop mode (continuous execution)
se3 run --loop

# Specify task type
se3 run "Fix bug" --type=bugfix

# Discovery mode
se3 run --discover "I want to build..."
```

**Task Types:**
| Type | Description | Steps |
|------|-------------|-------|
| `feature` | New functionality | Full 11-step workflow |
| `bugfix` | Fixing a bug | Skip design step |
| `review` | Code review/analysis | analyze → read_spec → verify_spec → summarize |
| `small` | Minor fix/typo | analyze → implement → test → commit → summarize |
| `directive` | Following specific instructions | analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize |

#### Scenario: New task execution
- **WHEN** user executes `se3 run "Implement user authentication"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: Resume interrupted flow
- **WHEN** user executes `se3 run --resume` with an active flow
- **THEN** the flow engine loads the persisted state
- **AND** continues execution from the interrupted step

#### Scenario: Loop mode execution
- **WHEN** user executes `se3 run --loop`
- **THEN** the flow engine continuously finds and executes tasks
- **AND** discovers tasks from backlog or TODOs

#### Scenario: Discovery mode execution
- **WHEN** user executes `se3 run --discover "Idea"`
- **THEN** the flow engine starts in discovery mode
- **AND** explores requirements through multi-turn conversation
- **AND** proceeds to analyze after user confirmation

### Requirement: `se3 init` Command

The `se3 init` command SHALL initialize a new SE3 project with the standard directory structure.

**Interface:**
```bash
se3 init [--project-root PATH] [--name PROJECT_NAME] [--force]
```

**Created Structure:**
```
project/
├── se3.yaml              # Project configuration
└── se3/
    └── specs/
        └── base/
            └── spec.md   # Base project specification
```

#### Scenario: Initialize new project
- **GIVEN** a directory without SE3 configuration
- **WHEN** user runs `se3 init`
- **THEN** it creates se3.yaml, se3/specs/, and se3/specs/base/spec.md

#### Scenario: Initialize with custom name
- **GIVEN** a directory at /path/to/my-project
- **WHEN** user runs `se3 init --name "My Project"`
- **THEN** the base spec contains "My Project" as project name

### Requirement: `se3 guardrails` Command

The `se3 guardrails` command SHALL check spec files against SE3 Spec Guardrails.

**Interface:**
```bash
se3 guardrails <spec-file> [--original <original-file>]
```

**Guardrail Checks:**
1. **must_not_delete**: Detect deleted WHEN/THEN scenarios
2. **must_not_weaken**: Detect weakened language (SHALL → SHOULD, MUST → SHOULD)

#### Scenario: Detect spec violations
- **GIVEN** a modified spec file
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command compares with original spec
- **AND** reports any deleted requirements or weakened language

#### Scenario: No violations found
- **GIVEN** a spec file with no guardrail violations
- **WHEN** user runs `se3 guardrails <spec-file>`
- **THEN** the command reports success

## Command Summary

| Command | Purpose | Status |
|---------|---------|--------|
| `se3 run` | Unified workflow entry point | **Required** |
| `se3 init` | Initialize SE3 project structure | **Required** |
| `se3 guardrails` | Check spec against guardrails | **Required** |

## Error Codes

| Code | Description |
|------|-------------|
| 0 | Success |
| 1 | General error / Guardrails violation |
| 130 | Interrupted by user (Ctrl+C) |
