# spec-guardrails Specification

## Purpose

Define the guardrails that protect spec integrity during implementation. These rules ensure that requirements are not inappropriately weakened, deleted, or modified by agents implementing against them.

## Requirements

### Requirement: Prohibited Actions

Agents MUST NOT perform the following actions without explicit human approval:

1. **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
2. **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
3. **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

#### Scenario: Attempt to delete requirement
- **WHEN** an agent removes a SHALL requirement from a spec during implementation
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to weaken requirement
- **WHEN** an agent changes "SHALL" to "SHOULD" or "MUST" to "SHOULD"
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to modify implementing spec
- **WHEN** an agent modifies scenarios in a spec they are currently implementing
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Permitted Actions

Agents CAN perform the following actions:

1. **CAN ADD** new requirements
2. **CAN MODIFY** requirements they are not currently implementing (with a change proposal)
3. **CAN MARK requirements as deprecated** with a human-approved reason and migration path

#### Scenario: Add new requirement
- **WHEN** an agent discovers a missing requirement during implementation
- **THEN** they CAN add the new requirement to the spec
- **AND** they SHOULD create a separate change to track the new requirement

#### Scenario: Modify non-implementing spec
- **WHEN** an agent identifies an issue in a spec they are NOT implementing
- **THEN** they CAN propose modifications through the normal change process

#### Scenario: Mark requirement as deprecated
- **WHEN** a requirement is no longer needed with human approval
- **THEN** the agent CAN mark it as deprecated with a reason and migration path

### Requirement: Guardrail Enforcement

The system SHALL enforce guardrails through automated checks.

**Enforcement points:**
1. Before committing, review the git diff of `se3/specs/`
2. If spec drift is detected, revert and investigate
3. Use `se3 guardrails` command to check spec files for violations

**Violation detection methods:**
1. Compare original and modified spec content
2. Check for deleted scenarios (missing WHEN clauses)
3. Check for weakened language (SHALL → SHOULD, MUST → SHOULD)
4. Check for weakened quantifiers (all → some, every → some)

#### Scenario: Automated guardrail check
- **WHEN** `se3 guardrails <spec-file>` is run
- **THEN** it compares the spec against the git HEAD version
- **AND** reports any violations of the guardrails

#### Scenario: Pre-archive check
- **WHEN** a change is about to be archived
- **THEN** the system checks for spec drift in `openspec/specs/`
- **AND** blocks archiving if violations are found

### Requirement: Guardrail Violation Reporting

When a guardrail violation is detected, the system SHALL provide clear reporting.

**Report format:**
- Violation type (delete/weaken/modify-implementing)
- File path and line number
- Original text (if applicable)
- Modified text (if applicable)
- Rule that was violated

#### Scenario: Report deletion violation
- **WHEN** a scenario is deleted from a spec
- **THEN** the report shows: "[must_not_delete] Deleted scenarios detected: {scenario_names}"

#### Scenario: Report weakening violation
- **WHEN** SHALL is changed to SHOULD
- **THEN** the report shows: "[must_not_weaken] Requirement weakened: SHALL → SHOULD"
