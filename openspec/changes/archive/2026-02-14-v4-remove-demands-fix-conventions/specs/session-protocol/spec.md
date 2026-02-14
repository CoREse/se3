## MODIFIED Requirements

### Requirement: Session Startup Protocol
The system SHALL define a progressive session startup protocol. The agent MUST locate current state with minimal context, then load more on demand.

Startup steps:
1. Read the latest entry in `progress.md` + `git log --oneline -5`
2. Scan `human-calls/` for responded but unprocessed requests
3. Check `openspec/changes/` for active changes and `openspec/specs/` for current capabilities
4. Determine session scope based on progress "next steps" + active changes
5. Load additional files only when the task requires them

If step 1 finds no progress.md and no git history → **first-time bootstrap**:
- Ask the human (sync human call): "What should this project do?"
- Create an openspec change from the response (proposal captures the intent)
- Create `progress.md`
- Initialize openspec if needed

#### Scenario: Mature project startup
- **WHEN** agent starts a session with existing progress.md and git history
- **THEN** agent reads only the latest progress entry and git log, then determines scope without reading all spec files upfront

#### Scenario: First-time bootstrap
- **WHEN** agent starts a session with no progress.md and no git history
- **THEN** agent asks the human what to build via human call, creates an openspec change from the response

#### Scenario: On-demand deep loading
- **WHEN** agent needs details about a specific capability during execution
- **THEN** agent reads the relevant spec file at that point, not during startup

### Requirement: Session Shutdown Protocol
Session ending MUST leave code in a mergeable state and update knowledge transfer files.

Shutdown steps:
1. Ensure all modified code runs correctly
2. Prepend this session's record to `progress.md`
3. Git commit with meaningful message (see commit convention below)
4. Update openspec change status if applicable

**Commit convention**: Commit when a meaningful unit of work is complete. A commit message MUST include a summary of changes and context for the next session. Do NOT commit just because of /new — only commit when there is something meaningful to record.

**Context clearing (/new)**: Clear context when it approaches saturation or when switching to a substantially different task. Do NOT mechanically clear after every task group — continue if there is context budget and the next task benefits from current context.

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
