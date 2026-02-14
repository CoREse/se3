# session-protocol Specification

## Purpose
Define the session lifecycle protocol for SE 3.0 agents. This spec governs progressive startup (status.md → progress.md → scope determination), execution boundaries, shutdown procedures, and progress tracking across sessions.
## Requirements
### Requirement: Session Startup Protocol
The system SHALL define a progressive session startup protocol. The agent MUST locate current state with minimal context, then load more on demand.

Startup steps:
1. Verify `openspec` CLI is available; if not, ask the human to install it. If available but `openspec/` directory does not exist, run `openspec init`.
2. Read `status.md` for current session state (runtime dashboard)
3. Read the latest entry in `progress.md` + `git log --oneline -5`
4. Scan `human-calls/` for responded but unprocessed requests
5. Check `openspec/changes/` for active changes and `openspec/specs/` for current capabilities
6. Determine session scope based on progress "next steps" + active changes
7. Load additional files only when the task requires them

If step 2 finds no progress.md and no git history → **first-time bootstrap**:
- Ask the human (sync human call): "What should this project do?"
- Create an openspec change from the response (proposal captures the intent)
- Create `progress.md`

#### Scenario: OpenSpec not installed
- **WHEN** agent starts a session and `openspec` command is not found
- **THEN** agent asks the human to install it via sync human call and does not proceed with spec-related work until resolved

#### Scenario: OpenSpec not initialized
- **WHEN** agent starts a session and `openspec` is available but `openspec/` directory does not exist
- **THEN** agent runs `openspec init` before proceeding

#### Scenario: Mature project startup
- **WHEN** agent starts a session with existing progress.md and git history
- **THEN** agent reads only the latest progress entry and git log, then determines scope without reading all spec files upfront

#### Scenario: First-time bootstrap
- **WHEN** agent starts a session with no progress.md and no git history
- **THEN** agent asks the human what to build via human call, creates an openspec change from the response

#### Scenario: On-demand deep loading
- **WHEN** agent needs details about a specific capability during execution
- **THEN** agent reads the relevant spec file at that point, not during startup

### Requirement: Session Execution Boundary
Each session MUST focus on a limited scope of work and MUST NOT attempt to complete too many tasks in a single session.

#### Scenario: Session scope limitation
- **WHEN** the agent has determined the work scope through the startup protocol
- **THEN** the agent only executes tasks within that scope and does not actively expand the scope

### Requirement: Session Shutdown Protocol
Session ending MUST leave code in a mergeable state and update knowledge transfer files.

Shutdown steps:
1. Ensure all modified code runs correctly
2. Prepend this session's record to `progress.md`
3. Git commit
4. Update openspec change status if applicable

### Requirement: Session Commit Cadence

The system SHALL define when commits occur during a session.

Commits SHOULD occur mid-session when a distinct, working unit of change is complete — not accumulated into a single commit with unrelated changes.

**When to commit mid-session:**
- After completing a coherent unit of work that passes tests
- Before starting a substantially different task that would muddy the commit message
- Before context clearing (/new) if there are completed changes to preserve

**Commit sequence:**
1. Run tests — do NOT commit with failing tests
2. Stage files
3. Write message with context for next session
4. Commit

**Commit Rules:**
- Commit when a meaningful unit of work is complete — not tied to /new or any mechanical trigger
- Do not batch unrelated changes into one commit
- Commit messages MUST include summary of changes and context for the next session

#### Scenario: Mid-session commit
- **WHEN** a distinct unit of work is complete and tested
- **THEN** agent commits before starting the next unit

### Requirement: Session Context Clearing

The system SHALL define when context is cleared during a session.

Context SHOULD be cleared when it approaches saturation or when switching to a substantially different task.

Context SHOULD NOT be cleared mechanically after every task group — agents SHOULD continue if there is context budget and the next task benefits from current context.

#### Scenario: Normal shutdown
- **WHEN** agent completes the current scope
- **THEN** agent executes shutdown protocol, code is mergeable, progress is updated

#### Scenario: Context approaching saturation
- **WHEN** context window is nearly full
- **THEN** agent prioritizes shutdown protocol (at minimum: commit current work + update progress.md)

### Requirement: Progress Tracking File
The system SHALL use progress.md as the cumulative cross-session progress record.

progress.md MUST contain:
- Reverse-chronological session records
- Each record: date, work summary, open issues, next steps

#### Scenario: Update progress
- **WHEN** session shutdown is executed
- **THEN** a new record is prepended to progress.md

