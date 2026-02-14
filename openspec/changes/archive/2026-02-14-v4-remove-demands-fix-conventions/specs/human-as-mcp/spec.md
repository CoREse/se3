## MODIFIED Requirements

### Requirement: Human Call Interface
The system SHALL define a standard human call interface supporting sync and async modes.

**Sync mode** (human present):
- Ask directly via Claude Code conversation (AskUserQuestion)
- Use for: immediate decisions, requirement clarification, project direction

**Async mode** (human unavailable):
- Write request file to `human-calls/`
- Use for: offline operations, cross-session pending requests

**First-time bootstrap**: When entering an empty project, the agent SHALL ask the human "What should this project do?" via sync human call, then create an openspec change directly from the response. The change proposal captures the human's intent; specs formalize it. No intermediate demands file.

#### Scenario: Sync call — first-time project intent
- **WHEN** agent enters an empty project
- **THEN** agent asks the human via sync call, creates an openspec change proposal from the response

#### Scenario: Sync call — immediate decision
- **WHEN** agent needs a human decision and the human is present
- **THEN** agent asks directly via conversation

#### Scenario: Async call — offline operation
- **WHEN** agent needs the human to perform an operation that requires offline time
- **THEN** agent writes a request to human-calls/, marks dependent tasks as waiting-human

#### Scenario: Human responds to async call
- **WHEN** a human fills in the Response section of a pending request
- **THEN** next session processes the response and unblocks dependent work

### Requirement: Non-Blocking Execution
Human calls MUST NOT block unrelated tasks.

#### Scenario: Continue after issuing call
- **WHEN** agent issues a human call and other independent tasks exist
- **THEN** agent marks dependent tasks as waiting-human and continues other work

### Requirement: Human Call Persistence
Async human call requests SHALL be persisted to the file system.

Files stored in `human-calls/` with filename format `{YYYYMMDD}-{HHmmss}-{short-description}.md`.

#### Scenario: Cross-session persistence
- **WHEN** session A's human call is unanswered at session end
- **THEN** the file persists and session B checks for responses on startup

### Requirement: Human Call Types
The system SHALL support three types: decision, action, information.

#### Scenario: Decision call
- **WHEN** agent needs the human to choose between options
- **THEN** the call lists all options with trade-off analysis
