# se3-scaffold Specification

## Purpose

Define the SE3 project scaffold system, including the CLAUDE.md + SE3.md template system, standard project structure, configuration system, and unified workflow via `se3 run`.

## Requirements

### Requirement: CLAUDE.md + SE3.md Template System

The system SHALL produce a two-part template system:
1. **CLAUDE.md** - Minimal project-level configuration file with core framework references
2. **SE3.md** - Complete framework implementation with all process definitions

The **CLAUDE.md template MUST include**:
- Core principles references
- Session protocol references
- Minimal project structure specification
- Reference to SE3.md for complete framework documentation

The **SE3.md template MUST include**:
- Complete standard process definitions (startup, execution, shutdown protocols)
- Detailed special file specifications (progress.md, human-calls/, etc.)
- Full conventional behavior definitions (self-iterate, change management, etc.)
- Human-as-MCP invocation specifications
- Agent Team collaboration specifications
- Verification protocol
- Spec guardrails
- Flow Engine workflow

#### Scenario: New project adopts SE3
- **WHEN** a user initializes SE3 framework in a new project
- **THEN** the system generates both CLAUDE.md (minimal) and SE3.md (complete) in the .claude/ directory

### Requirement: SE3 Project Structure

The system SHALL define the standard SE3 project file structure.

**Standard structure:**
```
project/
├── init.sh                # Environment setup (optional)
├── progress.md            # Cross-session progress tracking
├── se3.yaml               # Framework configuration (optional)
├── README.md              # Project documentation
├── specs/                 # Source of truth for requirements
│   ├── _changelog/        # Spec change log
│   └── <capability>/      # Capability specs
│       └── spec.md
├── .claude/               # Framework implementation (read-only for users)
│   ├── CLAUDE.md          # SE3 minimal framework reference (project-level)
│   ├── SE3.md             # Complete SE3 framework implementation
│   └── commands/          # Claude command definitions
└── se3/                   # SE3 runtime metadata and state (VISIBLE for human-as-MCP)
    ├── calls/             # Human call queue
    │   ├── active/        # Pending human calls
    │   └── archive/       # Completed/archived calls
    ├── collab/            # Multi-agent collaboration state
    ├── tmp/               # Temporary files (auto-cleaned)
    └── state/             # Session state files
        └── engine.json    # Flow Engine state persistence
```

**Key Directories:**
- `specs/` - Spec files (migrated from `openspec/specs/`)
- `se3/` - Runtime state (intentionally NOT hidden for human discoverability)
- `.claude/` - Framework files (managed by se3 tool)

**Migration Notes:**
- `openspec/specs/` → `specs/` (SE3 3.0)
- `openspec/changes/` → managed via flow engine or archived
- `human-calls/` → `se3/calls/`
- `.collab/` → `se3/collab/`

#### Scenario: Project initialization
- **WHEN** SE3 is initialized in a directory
- **THEN** the standard file structure is created with `.claude/`, `se3/`, and `specs/` directories

#### Scenario: Migration from legacy structure
- **WHEN** a project has legacy directories (`human-calls/`, `.collab/` in root)
- **THEN** `se3 migrate` moves them to `se3/` structure
- **AND** preserves all existing data

### Requirement: Configuration System

The system SHALL support configuring framework behavior via `se3.yaml` (with legacy fallback to `se3.config.yaml`).

**Configuration options:**
- `max_tasks_per_change`: Maximum tasks per change (default: 5)
- `human_call.timeout_days`: Default timeout days for human calls (default: 7)
- `agent_team.roles`: List of enabled agent roles
- `session.max_progress_entries`: Maximum session records to keep in progress (default: 20)
- `flow_engine.default_task_type`: Default task type for `se3 run` (default: feature)

#### Scenario: Using default configuration
- **WHEN** no se3.yaml file exists in the project
- **THEN** the framework runs with built-in default values

### Requirement: SE3.md Generation via se3 init

The system SHALL generate SE3.md file via the `se3 init` command.

The `se3 init` command MUST:
1. Create the .claude/ directory if it doesn't exist
2. Generate SE3.md with the complete framework implementation
3. Generate CLAUDE.md with minimal framework references
4. Ensure both files are properly formatted
5. Preserve existing files if they already exist

#### Scenario: SE3.md generation on initialization
- **WHEN** a user runs `se3 init` in a project directory
- **THEN** the system creates .claude/SE3.md with the complete framework and .claude/CLAUDE.md with minimal content

### Requirement: Output Artifacts

The system SHALL produce the following deliverables:
1. Project-level CLAUDE.md template (minimal, English)
2. Project-level SE3.md template (complete, English)
3. Configuration file template (se3.yaml)
4. Documentation and best practices guide
5. CLI tools documentation

#### Scenario: Complete delivery
- **WHEN** SE3 framework design is complete
- **THEN** all deliverables are available for direct use in new projects

### Requirement: Temporary File Management

The system SHALL manage temporary files to prevent root directory pollution.

**Temporary file locations:**
- All temporary files MUST be created in `se3/tmp/` instead of project root
- Temporary files include: session buffers, intermediate outputs

**Cleanup policy:**
- Flow completion automatically cleans `se3/tmp/` files older than 7 days
- Files in root are considered legacy and SHOULD be migrated

**Git ignore:**
- The CLAUDE.md template MUST include `se3/tmp/` in `.gitignore` recommendations

#### Scenario: Temporary file creation
- **WHEN** a tool needs to create a temporary file
- **THEN** it creates it in `se3/tmp/` with a unique name
- **AND** the file is automatically cleaned up after session ends or per retention policy

### Requirement: CLI Tools

The system SHALL provide CLI tools for validating and enforcing SE3 conventions.

**Core Tools:**
- `se3 run` — Unified workflow entry point (SE3 3.0+)
- `se3 status` — Diagnose session state and identify issues
- `se3 commit` — Commit changes with test verification and sensitive file checks
- `se3 handoff` — Generate session summary and end flow

**Quality Tools:**
- `se3 lint` — Validate spec file format and content
- `se3 verify` — Verify change implementation covers all spec scenarios
- `se3 guardrails` — Check spec integrity

**Maintenance Tools:**
- `se3 health` — Check SE3 system integrity
- `se3 migrate` — Migrate legacy directory structures
- `se3 init` — Initialize a new SE3 project
- `se3 update` — Update SE3.md to the latest framework version

#### Scenario: Spec validation
- **WHEN** a developer runs `se3 lint`
- **THEN** the tool validates all specs and reports any format violations

#### Scenario: Change verification
- **WHEN** an agent completes implementing a change
- **THEN** `se3 verify` confirms all scenarios are covered

### Requirement: Self-Iterate Flow

The system SHALL define a self-iterate behavior that drives the project from human intent to working implementation.

**Flow via `se3 run --loop`:**
1. Obtain direction via human call → create flow
2. Execute flow through Flow Engine (analyze → ... → summarize)
3. Check if more tasks exist in backlog/roadmap
4. If yes: continue to next task
5. If no: exit loop

#### Scenario: Self-iterate execution
- **WHEN** agent runs `se3 run --loop`
- **THEN** agent executes flows continuously until no more tasks

### Requirement: Change Lifecycle Management

The system SHALL define a complete change lifecycle managed by the Flow Engine.

**Flow-Based Changes:**
- Changes are tracked within flow instances
- Each flow has a unique ID and associated metadata
- Flow state persists in `se3/state/engine.json`
- Completed flows generate summaries in `progress.md`

**Legacy Change Directory Structure (deprecated):**
```
openspec/changes/
├── active-change/          # Active change with .openspec.yaml
│   ├── .openspec.yaml      # Change metadata
│   ├── proposal.md         # Change proposal
│   ├── tasks.md            # Implementation tasks
│   └── specs/              # Optional: specs created/modified
└── archive/                # Archived changes
    └── YYYY-MM-DD-change-name/
```

**Modern Flow-Based Tracking:**
- Flow instances tracked in `se3/state/engine.json`
- Summaries stored in `se3/state/summary-<flow-id>.md` (Markdown format)
- `progress.md` aggregates completed flows

#### Scenario: Create new flow
- **WHEN** `se3 run "Implement feature X"` is executed
- **THEN** a new flow is created with unique ID
- **AND** state is persisted after each step

#### Scenario: Archive completed flow
- **WHEN** a flow reaches summarize step
- **THEN** a summary is generated
- **AND** the flow is marked COMPLETED

### Requirement: Spec Directory Structure

The system SHALL define the specs directory structure.

**Specs Location:**
- Primary: `specs/` (SE3 3.0+)
- Legacy fallback: `openspec/specs/` (for backward compatibility)

**Spec Organization:**
```
specs/
├── _changelog/             # Spec change log
│   └── YYYY-MM-DD-change.md
├── flow-engine/            # Core flow engine spec
│   └── spec.md
├── se3-commands/           # CLI commands spec
│   └── spec.md
├── se3-workflows/          # Workflow definitions
│   └── spec.md
└── ...
```

**Spec Format:**
- Markdown format
- Required sections: Purpose, Requirements
- Scenario format: WHEN/THEN

#### Scenario: Spec discovery
- **WHEN** flow engine reads specs
- **THEN** it looks in `specs/` first, then `openspec/specs/` as fallback
