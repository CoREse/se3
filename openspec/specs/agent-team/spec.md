# agent-team Specification

## Purpose
Define the multi-agent task coordination system using Claude Code's native Task tool. This spec governs how parent agents distribute work to sub-agents, how role differentiation works through prompts, and how change-level isolation enables safe parallel execution.
## Requirements
### Requirement: Multi-Agent Task Coordination
The system SHALL support multi-agent parallel work using Claude Code's native Task tool with sub-agents.

Parent agents distribute work by spawning sub-agents via `Task` tool with appropriate `subagent_type`. Sub-agents operate on the same file system and return results directly to the parent. No file-based communication channel is needed.

Isolation is achieved through openspec changes — each sub-agent works on a different change, naturally touching different files.

#### Scenario: Task distribution via native Task tool
- **WHEN** a project has multiple independent openspec changes to implement
- **THEN** the parent agent spawns sub-agents via Task tool, each assigned a different change

#### Scenario: Conflict avoidance
- **WHEN** multiple sub-agents work in parallel
- **THEN** change-level isolation ensures agents do not modify the same files simultaneously

### Requirement: Agent Role Differentiation
The system SHALL support agent role differentiation through Task tool prompts.

Roles are expressed in the prompt given to sub-agents, not through separate configuration files:
- **architect**: Responsible for spec design, change proposals, architecture decisions
- **implementer**: Implements code according to specs and design
- **reviewer**: Verifies implementation matches specs

#### Scenario: Role assignment via prompt
- **WHEN** a parent agent spawns a sub-agent for implementation work
- **THEN** the prompt specifies the role (e.g., "As an implementer, execute tasks 1-3 of change X")

### Requirement: Git Worktree Collaboration Mode

The system SHALL support a Git Worktree Collaboration mode for long-running multi-agent collaboration with full isolation and independent context windows.

**Architecture:**
- **Orchestrator** (bash): Manages task state, health checks, launches manager/worker processes
- **Manager** (`kclaude -p`): Analyzes state, creates tasks, reviews work, makes merge decisions
- **Worker** (`kclaude -p`): Implements tasks in isolated git worktrees

**Directory Structure:**
```
.collab/
├── config.json           # session configuration
├── tasks/                # task definitions (task-*.json)
├── logs/                 # manager/worker logs
└── events/               # event queue

.worktrees/
└── {task-id}/           # per-task git worktrees
```

**Task State Machine:**
```
pending → in_progress → done/failed/timeout/blocked/escalated
```

**Launch Modes:**

1. **Daemon mode** (`--daemon`): Fully automatic, orchestrator manages everything
2. **Manual mode** (`--manual`): Generate task files, user launches manager/worker manually
3. **Direct mode** (default): Run orchestrator in foreground (for testing)

**CLI Commands:**
```bash
se3 collab --daemon "Implement feature X"          # Start automatic collaboration
se3 collab --manual "Implement feature X"          # Generate plan, manual execution
se3 collab --launch-manager plan                   # Launch manager for event
se3 collab --launch-worker task-001                # Launch worker for task
se3 collab --status                                # Check session status
se3 collab --abort                                 # Stop and cleanup
```

#### Scenario: Start daemon collaboration
- **WHEN** user runs `se3 collab --daemon "Implement feature X"`
- **THEN** the orchestrator starts and manages the full collaboration automatically

#### Scenario: Check collab status
- **WHEN** user runs `se3 collab --status`
- **THEN** it shows the current collaboration state including active tasks

### Requirement: Mode Selection

The system SHALL automatically select the appropriate mode based on the work:

- **Task Tool Mode** (default): Use for most work, single agent or parallel independent changes
- **Git Worktree Mode**: Use for long-running multi-agent collaboration requiring full isolation

#### Scenario: Default to Task Tool mode
- **WHEN** no special collaboration needs are detected
- **THEN** use Task Tool mode as the default

#### Scenario: Select Git Worktree mode for complex collaboration
- **WHEN** long-running multi-agent collaboration is needed
- **THEN** use Git Worktree mode for better isolation

