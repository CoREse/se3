# se3-scaffold Specification

## Purpose
Define the SE 3.0 project scaffold system, including the CLAUDE.md template system, standard project structure, configuration system, and self-iterate workflow. This spec governs how SE 3.0 generates its output artifacts and how new projects adopt the framework.
## Requirements
### Requirement: CLAUDE.md Template System
The system SHALL produce a reusable CLAUDE.md template as the primary implementation vehicle for the SE 3.0 framework on Claude Code.

The template MUST include:
- Standard process definitions (startup, execution, shutdown protocols)
- Special file specifications (status.md, progress.md, human-calls/, etc.)
- Conventional behavior definitions (self-iterate, change management, etc.)
- Human-as-MCP invocation specifications
- Agent Team collaboration specifications

#### Scenario: New project adopts SE 3.0
- **WHEN** a user initializes SE 3.0 framework in a new project
- **THEN** the system generates the complete CLAUDE.md template and supporting file structure

### Requirement: SE 3.0 Project Structure
The system SHALL define the standard SE 3.0 project file structure.

Standard structure:
```
project/
├── init.sh                # Environment setup (optional)
├── status.md              # Runtime dashboard (current session state)
├── progress.md            # Cross-session progress tracking
├── se3.config.yaml        # Framework configuration (optional)
├── README.md              # Project documentation
├── human-calls/           # Async human call queue
├── openspec/
│   ├── specs/             # Source of truth for requirements
│   └── changes/
│       └── archive/
└── .claude/
    └── CLAUDE.md          # SE 3.0 framework (project-level)
```

OpenSpec specs serve as the single source of truth for project requirements. No separate demands/requirements file is needed.

#### Scenario: Project initialization
- **WHEN** SE 3.0 is initialized in a directory
- **THEN** the standard file structure is created without demands.md

### Requirement: Configuration System
The system SHALL support configuring framework behavior via `se3.config.yaml`.

Configuration options include:
- `max_tasks_per_change`: Maximum tasks per change (default: 5)
- `human_call.timeout_days`: Default timeout days for human calls (default: 7)
- `agent_team.roles`: List of enabled agent roles
- `session.max_progress_entries`: Maximum session records to keep in progress (default: 20)

#### Scenario: Using default configuration
- **WHEN** no se3.config.yaml file exists in the project
- **THEN** the framework runs with built-in default values

### Requirement: Output Artifacts
The system SHALL produce the following deliverables:
1. Project-level CLAUDE.md template (English)
2. Global CLAUDE.md template for ~/.claude/CLAUDE.md (English)
3. Configuration file template
4. Documentation and best practices guide
5. CLI tools documentation (TOOLS.md)

#### Scenario: Complete delivery
- **WHEN** SE 3.0 framework design is complete
- **THEN** all deliverables are available for direct use in new projects

### Requirement: CLI Tools
The system SHALL provide CLI tools for validating and enforcing SE 3.0 conventions.

Tools include:
- `se3 lint` — Validate spec file format and content
- `se3 sync` — Synchronize output/ directory with source files
- `se3 verify` — Verify change implementation covers all spec scenarios
- `se3 status` — Diagnose session state and identify issues

#### Scenario: Spec validation
- **WHEN** a developer runs `se3 lint`
- **THEN** the tool validates all specs and reports any format violations

#### Scenario: Change verification
- **WHEN** an agent completes implementing a change
- **THEN** `se3 verify --change <name>` confirms all scenarios are covered before archiving

### Requirement: Self-Iterate Flow
The system SHALL define a self-iterate behavior that drives the project from human intent to working implementation.

Flow:
1. Obtain direction via human call → create openspec change (proposal captures the intent)
2. Implement the change (specs → design → tasks → code)
3. Verify implementation against specs
4. Check if specs fully cover the project goals — if gaps exist, go to 1
5. Update project documentation

#### Scenario: Self-iterate execution
- **WHEN** agent is instructed to self-iterate
- **THEN** agent executes the flow without stopping until step 5, using human calls only when blocked

