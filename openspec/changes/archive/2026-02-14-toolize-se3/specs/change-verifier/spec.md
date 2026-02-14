# change-verifier Specification

## Purpose
Define the pre-archive verification tool that ensures implementation covers all spec scenarios before a change is archived.

## ADDED Requirements

### Requirement: Spec Scenario Extraction
The system SHALL extract all WHEN/THEN scenarios from a change's specs.

#### Scenario: Extract scenarios from change
- **WHEN** `se3 verify --change <name>` is run
- **THEN** it parses all spec files in `openspec/changes/<name>/specs/`
- **AND** extracts a list of scenario IDs (format: `<spec-name>/<scenario-name>`)

### Requirement: Implementation Detection
The system SHALL detect evidence of scenario implementation.

Detection methods (in order of confidence):
1. Test file with `@pytest.mark.scenario("<id>")` decorator
2. Code comment `# Verify: <scenario-id>`
3. Spec archive with "implemented" marker

#### Scenario: Find test marker
- **WHEN** a test file contains `@pytest.mark.scenario("session-protocol/first-time-bootstrap")`
- **THEN** `se3 verify` marks that scenario as covered

### Requirement: Coverage Report
The system SHALL generate a coverage report showing implemented vs missing scenarios.

#### Scenario: Incomplete implementation
- **WHEN** some scenarios have no detected implementation
- **THEN** `se3 verify` lists uncovered scenarios with file paths
- **AND** exits with code 1

#### Scenario: Complete implementation
- **WHEN** all scenarios have detected implementation
- **THEN** `se3 verify` reports success
- **AND** exits with code 0

### Requirement: Skip Annotation
The system SHALL support explicitly skipping scenarios.

#### Scenario: Skip with reason
- **WHEN** a spec contains `<!-- verify-skip: reason -->` before a scenario
- **THEN** that scenario is excluded from coverage requirements
