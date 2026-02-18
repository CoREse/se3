# session-protocol Specification

## Purpose
Define the session lifecycle protocol for SE 3.0 agents. This spec governs progressive startup (se3 status → progress.md → scope determination), execution boundaries, shutdown procedures, and tool-enforced progress tracking across sessions.
## Requirements
### Requirement: Session Startup Protocol
The system SHALL define a progressive session startup protocol. The agent MUST locate current state with minimal context, then load more on demand.

Startup steps:
1. Verify `openspec` CLI is available; if not, ask the human to install it. If available but `openspec/` directory does not exist, run `openspec init`.
2. Run `se3 status` for computed project state (git, collab, human-calls)
3. Read `head -50 progress.md` + `git log --oneline -5`
4. Scan `human-calls/` for responded but unprocessed requests
5. Check `openspec/changes/` for active changes and `openspec/specs/` for current capabilities
6. Determine session scope based on progress "next steps" + active changes
7. Load additional files only when the task requires them

If step 3 finds no progress.md and no git history → **first-time bootstrap**:
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
- **THEN** agent runs `se3 status`, reads progress.md and git log, then determines scope without reading all spec files upfront

#### Scenario: First-time bootstrap
- **WHEN** agent starts a session with no progress.md and no git history
- **THEN** agent asks the human what to build via human call, creates an openspec change from the response

#### Scenario: On-demand deep loading
- **WHEN** agent needs details about a specific capability during execution
- **THEN** agent reads the relevant spec file at that point, not during startup

### Requirement: Input Classification and Stage Routing

The system SHALL classify user input to determine the appropriate workflow stage.

**Intent Types:**
| Intent Type | Description | Stage Entry |
|-------------|-------------|-------------|
| `directive` | Explicit self-iterate, "implement X", "start feature Y" | Full SDD workflow |
| `bug-report` | Error description, stack trace, broken behavior | Bug fix workflow |
| `feature-request` | New capability, enhancement idea | Feature proposal workflow |
| `question` | How does X work? Why Y? | Knowledge query |
| `review` | "Check this", "What do you think", "Is this correct" | Review workflow |
| `clarification` | Follow-up on previous topic | Resume/continue workflow |
| `meta` | About the project/process itself | Meta workflow |
| `off-topic` | Not related to project | Answer without modifying project files |

**Classification Indicators:**
- Bug: "error", "bug", "broken", "fail", "crash", "exception", "stack trace", "not working"
- Review: "review", "check this", "look at", "what do you think", "is this correct"
- Feature: "add ", "implement", "create ", "build ", "support ", "feature", "new capability"
- Question: "how ", "why ", "what is", "explain", "?"
- Directive: "self-iterate", "continue", "proceed", "start ", "fix ", "update ", "refactor "

**Stage Decision Matrix:**
- IF intent == bug-report: Route to bugfix workflow (create change if needed)
- IF intent == feature-request: Route to feature workflow (or small workflow if simple)
- IF intent == review: Route to review workflow
- IF intent == question: Explore and answer (no change created)
- IF intent == directive: Execute with SDD workflow
- IF intent == clarification: Continue previous context

#### Scenario: Bug report classification
- **WHEN** user input contains bug indicators like "error", "broken", "not working"
- **THEN** system classifies intent as "bug-report" and routes to bugfix workflow

#### Scenario: Feature request classification
- **WHEN** user input contains feature indicators like "add", "implement", "create"
- **THEN** system classifies intent as "feature-request" and routes to feature workflow

#### Scenario: Review request classification
- **WHEN** user input contains review indicators like "review", "check this"
- **THEN** system classifies intent as "review" and routes to review workflow

### Requirement: Session Execution Boundary
Each session MUST focus on a limited scope of work and MUST NOT attempt to complete too many tasks in a single session.

#### Scenario: Session scope limitation
- **WHEN** the agent has determined the work scope through the startup protocol
- **THEN** the agent only executes tasks within that scope and does not actively expand the scope

### Requirement: Session Shutdown Protocol
Session ending MUST leave code in a mergeable state. Progress tracking is handled automatically by tools.

Shutdown steps:
1. Ensure all modified code runs correctly
2. Run `se3 handoff` — automatically commits and generates session summary in progress.md
3. Update openspec change status if applicable

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
4. Commit (via `se3 commit` — automatically appends progress entry)

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
- **THEN** agent runs `se3 handoff`, code is mergeable, progress is updated automatically

#### Scenario: Context approaching saturation
- **WHEN** context window is nearly full
- **THEN** agent prioritizes shutdown protocol (at minimum: `se3 commit` current work)

### Requirement: Progress Tracking File
The system SHALL use progress.md as the cumulative cross-session progress record. Progress entries are appended automatically by `se3 commit` and `se3 handoff` — agents do NOT manually maintain progress.md.

progress.md contains:
- Auto-appended commit entries in "Current Session" section (by `se3 commit`)
- Formal session records generated by `se3 handoff`
- Collab session reports generated by orchestrator do_complete()

#### Scenario: Auto-append commit entry
- **WHEN** `se3 commit` succeeds
- **THEN** a one-line commit record is automatically appended to progress.md Current Session section

#### Scenario: Finalize session
- **WHEN** `se3 handoff` is executed
- **THEN** the Current Session section is replaced with a formal dated session record including commits, files changed, and next steps

### Requirement: Session Guard

The system SHALL enforce session validity checks before allowing session-aware operations.

Session-aware commands (`se3 work`, `se3 done`) SHALL check if session is properly started:
- If `.claude/.session.json` does not exist → error `SESSION_NOT_STARTED`
- If session status is not "active" → error `SESSION_NOT_ACTIVE`
- If session file is corrupted → error `SESSION_INVALID`

**Resolution:**
- In all error cases, agent should run `se3 start` first

#### Scenario: Work without session
- **WHEN** `se3 work` runs without `.claude/.session.json`
- **THEN** it returns `SESSION_NOT_STARTED` error
- **AND** agent runs `se3 start` to initialize

#### Scenario: Done with inactive session
- **WHEN** `se3 done` runs with non-active session
- **THEN** it returns `SESSION_NOT_ACTIVE` error
- **AND** agent must start a session first

#### Scenario: Corrupted session file
- **WHEN** `.claude/.session.json` exists but is malformed
- **THEN** it returns `SESSION_INVALID` error
- **AND** agent must reinitialize the session

