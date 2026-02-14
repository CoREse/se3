## MODIFIED Requirements

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
