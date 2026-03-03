# se3-workflows Specification

## Purpose

Define the standard workflows for SE3 development: bug fix, feature, review, directive, and small workflows. These workflows govern how different types of work are processed from intake to completion.

## Requirements

### Requirement: Workflow Types

The system SHALL support six workflow types:

| Type | Steps | When Used |
|------|-------|-----------|
| `discovery` | explore → clarify → confirm | Vague ideas, requirements unclear |
| `bugfix` | analyze → fix → verify | Bug reports |
| `feature` | clarify → propose → spec → design → implement → verify | Feature requests |
| `review` | inspect → report → fix | Code review requests |
| `directive` | plan → implement → verify → check_coverage | "Implement X" commands |
| `small` | implement → verify | Simple changes, no openspec needed |

#### Scenario: Bug fix workflow selection
- **WHEN** input is classified as "bug-report"
- **THEN** the system uses the bugfix workflow

#### Scenario: Feature workflow selection
- **WHEN** input is classified as "feature-request"
- **THEN** the system uses the feature workflow

#### Scenario: Small workflow selection
- **WHEN** the change is simple (no spec changes needed, ≤3 tasks)
- **THEN** the system uses the small workflow for efficiency

### Requirement: Discovery Workflow

The discovery workflow SHALL be used when the user has a vague idea but needs help clarifying requirements.

**Steps:**

**1. EXPLORE**
   - Ask clarifying questions based on initial description
   - Understand the problem space and constraints
   - Identify stakeholders and requirements

**2. CLARIFY**
   - Synthesize understanding from conversation
   - Generate refined task description
   - Present to user for confirmation

**3. CONFIRM**
   - Wait for user approval or feedback
   - If feedback provided: iterate back to EXPLORE
   - If approved: proceed with refined description

**Interface:**
```bash
se3 run --discover "I want to build something related to user management"
```

**Features:**
- Multi-turn conversation with pause/resume support
- Maximum 10 rounds to prevent infinite loops
- Conversation history preserved in flow state
- Refined description automatically passed to analyze step

#### Scenario: Discovery mode entry
- **WHEN** user executes `se3 run --discover "vague idea"`
- **THEN** the system enters discovery workflow
- **AND** starts multi-turn conversation to clarify requirements

#### Scenario: Discovery synthesis
- **GIVEN** several rounds of clarifying questions
- **WHEN** the system has enough information
- **THEN** it generates a refined task description
- **AND** pauses for user confirmation

#### Scenario: Discovery to feature transition
- **GIVEN** user confirms the refined description
- **WHEN** discovery workflow completes
- **THEN** the system automatically proceeds to feature workflow
- **AND** uses the refined description as input

### Requirement: Bug Fix Workflow

The bugfix workflow SHALL follow these steps:

**1. ANALYZE**
   - Reproduce the bug
   - Identify root cause
   - Determine affected components

**2. FIX**
   - IF complexity > small:
     - Create openspec/change/bugfix-{id}/
     - Write fix-spec.md: expected behavior, test cases
     - Implement fix
     - Run tests to verify
   - ELSE:
     - Fix directly
     - Run tests

**3. VERIFY**
   - Confirm bug is resolved
   - Run regression tests
   - Update relevant specs if behavior changed
   - Archive change (if created)

#### Scenario: Complex bug fix
- **WHEN** a bug requires significant changes
- **THEN** create a formal openspec change with fix-spec.md
- **AND** follow the full bugfix workflow

#### Scenario: Simple bug fix
- **WHEN** a bug is small and easily fixed
- **THEN** fix directly without creating a formal change
- **AND** run tests to verify

### Requirement: Feature Request Workflow

The feature workflow SHALL follow these steps:

**1. CLARIFY**
   - Understand the request
   - Ask clarifying questions
   - Determine scope and priority

**2. PROPOSE**
   - Create openspec/change/feature-{id}/
   - Write proposal.md: what, why, acceptance criteria
   - Get human approval (if significant)

**3. SPEC**
   - Write/update specs in openspec/specs/
   - Define scenarios (WHEN/THEN)
   - Run `se3 lint` to validate

**4. DESIGN** (if needed)
   - Write design.md for complex changes
   - Design architecture

**5. IMPLEMENT**
   - Break into tasks (max 5 per group)
   - Implement incrementally
   - Run tests continuously

**6. VERIFY**
   - Run all tests
   - Verify each spec scenario
   - Archive change

#### Scenario: Large feature
- **WHEN** a feature is complex with multiple components
- **THEN** go through all steps including design
- **AND** create formal specs and design documents

#### Scenario: Medium feature
- **WHEN** a feature is moderately complex
- **THEN** create proposal and specs but skip design

### Requirement: Review Workflow

The review workflow SHALL follow these steps:

**1. INSPECT**
   - Read the code/file in question
   - Check against specs
   - Identify issues

**2. REPORT**
   - Provide findings to human
   - Categorize: critical / warning / suggestion

**3. FIX** (optional, if requested)
   - IF fix approved: route to Bug Fix or Feature workflow
   - ELSE: end here

#### Scenario: Code review
- **WHEN** user asks for a review
- **THEN** inspect the code and report findings
- **AND** categorize issues by severity

### Requirement: Directive Workflow

The directive workflow SHALL follow these steps:

**1. PLAN**
   - Create openspec change from user direction
   - Determine scope and approach

**2. IMPLEMENT**
   - Execute the directive
   - Run tests continuously

**3. VERIFY**
   - Run all tests
   - Verify implementation

**4. CHECK_COVERAGE**
   - Check if specs fully cover project goals
   - If gaps exist, create new changes

#### Scenario: Self-iterate directive
- **WHEN** user says "self-iterate" or "continue"
- **THEN** use directive workflow
- **AND** check coverage at the end

### Requirement: Small Workflow

The small workflow SHALL be used for simple changes that don't need formal specs.

**Steps:**
1. IMPLEMENT - Direct code changes
2. VERIFY - Run tests

#### Scenario: Documentation update
- **WHEN** updating README or comments
- **THEN** use small workflow
- **AND** skip formal change creation

#### Scenario: Quick fix
- **WHEN** a one-line fix is needed
- **THEN** use small workflow for efficiency

### Requirement: Adaptive Formality

The system SHALL automatically determine formality based on change contents:

- **Large**: Has proposal + specs + design
- **Medium**: Has proposal + specs (no design)
- **Small**: No proposal/specs, ≤3 tasks

#### Scenario: Large change detection
- **WHEN** a change has proposal, specs, and design
- **THEN** formality is "large"

#### Scenario: Small change detection
- **WHEN** a change has no proposal/specs and ≤3 tasks
- **THEN** formality is "small"

### Requirement: Spec Guardrails

The system SHALL enforce guardrails that protect spec integrity during implementation. Agents MUST NOT perform the following actions without explicit human approval:

1. **MUST NOT delete** an existing spec requirement without explicit human approval (via human call)
2. **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
3. **MUST NOT modify** the description or scenarios of a requirement they are implementing — the implementer does not get to change the spec they're building against

**Permitted Actions:**
- **CAN ADD** new requirements
- **CAN MODIFY** requirements they are not currently implementing (with a change proposal)
- **CAN MARK requirements as deprecated** with a human-approved reason and migration path

**Enforcement points:**
1. Before archiving a change, review the git diff of `openspec/specs/`
2. If spec drift is detected, revert and investigate
3. Use `se3 guardrails` command to check spec files for violations

**Violation detection methods:**
1. Compare original and modified spec content
2. Check for deleted scenarios (missing WHEN clauses)
3. Check for weakened language (SHALL → SHOULD, MUST → SHOULD)
4. Check for weakened quantifiers (all → some, every → some)

#### Scenario: Attempt to delete requirement
- **WHEN** an agent removes a SHALL requirement from a spec during implementation
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to weaken requirement
- **WHEN** an agent changes "SHALL" to "SHOULD" or "MUST" to "SHOULD"
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to modify implementing spec
- **WHEN** an agent modifies scenarios in a spec they are currently implementing
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Add new requirement
- **WHEN** an agent discovers a missing requirement during implementation
- **THEN** they CAN add the new requirement to the spec
- **AND** they SHOULD create a separate change to track the new requirement

#### Scenario: Automated guardrail check
- **WHEN** `se3 guardrails <spec-file>` is run
- **THEN** it compares the spec against the git HEAD version
- **AND** reports any violations of the guardrails
