# change-verifier Specification

## Purpose
Define the verification protocol for SE3 changes, ensuring spec scenarios have corresponding implementation evidence. This spec governs scenario extraction, implementation detection through test markers and code comments, coverage reporting, and skip annotations to validate that changes are fully implemented.
## Requirements
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

### Requirement: Verification Protocol

The system SHALL define a comprehensive verification protocol to ensure changes are fully implemented and tested.

**The Rule**: Never mark a feature or change as complete without running tests that prove it works.

**Verification Methods (in order of preference):**

1. **Spec scenarios as acceptance criteria**: Each WHEN/THEN scenario in a spec is a test case. Before marking a change complete, verify every scenario.

2. **Automated tests**: Write tests for spec scenarios when possible. Run them. A passing test suite is the only reliable proof of completion.

3. **E2E testing for user-facing features**: Visual regression testing catches issues that unit tests miss.
   - Navigate to the feature URL
   - Screenshot the critical UI state
   - Compare with baseline or verify key elements exist
   - Test user flows: click → wait → screenshot → assert

4. **Manual verification as fallback**: If no automated testing is feasible, manually exercise the feature and document the result.

**Visual Verification Checklist:**
- Layout not broken (no overlapping elements)
- Critical text visible and not truncated
- Interactive elements clickable
- No console errors during interaction

**When to Run Tests:**
- **Startup**: Run existing tests to establish a baseline before making changes
- **After implementation**: Run tests for the specific change
- **Before commit**: Run the full test suite — do NOT commit if tests fail
- **Before archiving a change**: Verify all spec scenarios pass

#### Scenario: Verify with automated tests
- **WHEN** a change has spec scenarios with corresponding test markers
- **THEN** running the test suite validates the implementation
- **AND** all tests MUST pass before marking complete

#### Scenario: Verify with E2E testing
- **WHEN** implementing a user-facing feature
- **THEN** use E2E testing to verify visual appearance and user flows
- **AND** ensure no layout issues or console errors

#### Scenario: Verify with manual testing
- **WHEN** automated testing is not feasible
- **THEN** manually exercise the feature
- **AND** document the verification results
