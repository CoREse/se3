# status-diagnostics Specification

## Purpose
Provide real-time project status diagnostics computed from live state (git, collab, human-calls, openspec). Replaces the manual status.md file with computed output.
## Requirements
### Requirement: Live State Computation
The system SHALL compute project status in real-time from the file system and git, not from a manually maintained status.md file.

Sources:
- `git status` / `git log` for branch, uncommitted changes, recent activity
- `openspec/changes/` for active (non-archived) changes
- `.collab/config.json` + `.collab/tasks/*.json` for collaboration session state
- `human-calls/` for pending/responded requests

#### Scenario: Compute git status
- **WHEN** `se3 status` is run
- **THEN** it shows current branch, uncommitted change count, and recent commits from live git state

#### Scenario: Detect active changes
- **WHEN** `se3 status` is run and `openspec/changes/` has non-archived directories
- **THEN** it lists them as active changes

#### Scenario: Show collab status
- **WHEN** `se3 status` is run and `.collab/config.json` exists with status "active"
- **THEN** it shows collab objective and task statuses

### Requirement: Human-Calls Check
The system SHALL check `human-calls/` for pending or responded requests.

#### Scenario: Unprocessed response
- **WHEN** a human-call file has `status: responded` but has not been processed
- **THEN** `se3 status` warns that response may need processing

#### Scenario: Long-pending call
- **WHEN** a human-call has been pending for > timeout_days
- **THEN** `se3 status` flags it as potentially stale

### Requirement: Collab Task Diagnostics
The system SHALL check collab task states for issues.

#### Scenario: Failed collab tasks
- **WHEN** collab tasks have status "failed" or "escalated"
- **THEN** `se3 status` warns about them with task IDs

#### Scenario: Blocked collab tasks
- **WHEN** collab tasks have status "blocked"
- **THEN** `se3 status` warns and suggests checking blocked_reason

### Requirement: Diagnostic Output
The system SHALL provide actionable diagnostic output.

Output format options:
- `--format=text` (default): Human-readable report
- `--format=json`: Machine-parseable for automation

#### Scenario: Healthy project
- **WHEN** all checks pass
- **THEN** `se3 status` reports "All diagnostics passed"

#### Scenario: Issues found
- **WHEN** one or more checks find issues
- **THEN** `se3 status` lists each issue with severity and suggested fix

