# status-diagnostics Specification

## Purpose
TBD - created by archiving change toolize-se3. Update Purpose after archive.
## Requirements
### Requirement: Status File Discovery
The system SHALL locate and read `status.md` from the project root.

#### Scenario: Find status file
- **WHEN** `se3 status` is run
- **THEN** it reads `./status.md` and parses its structured content

### Requirement: State Consistency Check
The system SHALL cross-check status.md against actual project state.

Checks:
- `Active Change` should match existing directory in `openspec/changes/`
- `Status: ready` should have empty Blockers table
- `Status: blocked` should have non-empty Blockers table with valid entries
- Git status should not have uncommitted changes if Status is `ready`

#### Scenario: Mismatched active change
- **WHEN** status.md shows Active Change "foo" but `openspec/changes/foo/` doesn't exist
- **THEN** `se3 status` reports inconsistency

#### Scenario: Stale blockers
- **WHEN** status is `blocked` but no blockers are listed
- **THEN** `se3 status` warns about malformed status

### Requirement: Human-Calls Check
The system SHALL check `human-calls/` for pending or responded requests.

#### Scenario: Unprocessed response
- **WHEN** a human-call file has `status: responded` but no recent progress.md entry
- **THEN** `se3 status` warns that response may need processing

#### Scenario: Long-pending call
- **WHEN** a human-call has been pending for > timeout_days
- **THEN** `se3 status` flags it as potentially stale

### Requirement: Diagnostic Output
The system SHALL provide actionable diagnostic output.

Output format options:
- `--format=text` (default): Human-readable table
- `--format=json`: Machine-parseable for automation

#### Scenario: Healthy project
- **WHEN** all checks pass
- **THEN** `se3 status` reports "All diagnostics passed" with green checkmarks

#### Scenario: Issues found
- **WHEN** one or more checks fail
- **THEN** `se3 status` lists each issue with severity and suggested fix

