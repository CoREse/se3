# se3-workflows Specification

## Purpose

Define the standard workflows for SE3 development using the Flow Engine's 11-step state machine. These workflows govern how different types of work are processed from intake to completion through the unified `se3 run` entry point.

## Requirements

### Requirement: Workflow Types

The system SHALL support five workflow types, mapped to different step sequences from the 11-step pool:

| Type | Steps | When Used |
|------|-------|-----------|
| `feature` | analyze → read_spec → propose → design → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize | New functionality or significant enhancement |
| `bugfix` | analyze → read_spec → propose → plan_tasks → implement → test → verify_spec → update_spec → commit → summarize | Bug reports (skip design for faster iteration) |
| `review` | analyze → read_spec → verify_spec → summarize | Code review, audit, or analysis |
| `small` | analyze → implement → test → commit → summarize | Minor fixes, typos, simple changes |
| `directive` | analyze → read_spec → plan_tasks → implement → test → verify_spec → commit → summarize | Following specific instructions |

**Step Pool (11 steps):**
1. **analyze** - Analyze task type and scope
2. **read_spec** - Read relevant specifications
3. **propose** - Generate change proposal
4. **design** - Design solution and architecture
5. **plan_tasks** - Break down into concrete tasks
6. **implement** - Write code implementation
7. **test** - Run tests to verify
8. **verify_spec** - Check implementation vs spec
9. **update_spec** - Update spec records
10. **commit** - Commit changes
11. **summarize** - Generate summary and handoff

#### Scenario: Feature workflow selection
- **WHEN** input is classified as "feature-request"
- **THEN** the system uses the feature workflow with full 11 steps

#### Scenario: Bug fix workflow selection
- **WHEN** input is classified as "bug-report"
- **THEN** the system uses the bugfix workflow (skips design step)

#### Scenario: Small workflow selection
- **WHEN** the change is simple (no spec changes needed, ≤3 tasks)
- **THEN** the system uses the small workflow for efficiency

### Requirement: Feature Workflow

The feature workflow SHALL follow these steps:

**1. ANALYZE**
   - Classify task type (feature/bugfix/review/small/directive)
   - Determine scope and complexity
   - Select appropriate step sequence

**2. READ_SPEC**
   - Automatically discover relevant specs based on scope
   - Load spec content into context

**3. PROPOSE**
   - Generate change proposal
   - Identify files to modify/create
   - Define acceptance criteria

**4. DESIGN**
   - Create design document for complex changes
   - Define architecture decisions
   - Design component interfaces

**5. PLAN_TASKS**
   - Break implementation into concrete tasks (max 5 per group)
   - Estimate complexity for each task
   - Define verification criteria

**6. IMPLEMENT**
   - Write code following the design
   - Include tests where applicable
   - Follow project conventions

**7. TEST**
   - Run test suite automatically
   - Report test results
   - Continue even if tests fail (verify_spec handles decision)

**8. VERIFY_SPEC**
   - Check implementation against specifications
   - Verify all scenarios are covered
   - Identify any discrepancies

**9. UPDATE_SPEC**
   - Update specs to reflect changes made
   - Add new capabilities documentation
   - Mark scenarios as implemented

**10. COMMIT**
   - Stage and commit all changes
   - Generate meaningful commit message
   - Auto-append to progress.md

**11. SUMMARIZE**
   - Generate session summary
   - Document changes made
   - Provide handoff context for future sessions

#### Scenario: Large feature
- **WHEN** a feature is complex with multiple components
- **THEN** go through all 11 steps including full design
- **AND** create formal specs and design documents

#### Scenario: Medium feature
- **WHEN** a feature is moderately complex
- **THEN** create proposal and specs but may skip design if simple

### Requirement: Bug Fix Workflow

The bugfix workflow SHALL follow these steps (skipping design):

**1. ANALYZE**
   - Reproduce the bug
   - Identify root cause
   - Determine affected components

**2. READ_SPEC**
   - Read relevant specs for context

**3. PROPOSE**
   - Generate fix proposal
   - Identify files to modify

**4. PLAN_TASKS** (if needed)
   - Break complex fixes into tasks
   - Skip for simple one-line fixes

**5. IMPLEMENT**
   - Fix the bug
   - Add regression tests

**6. TEST**
   - Run tests to verify fix
   - Run regression tests

**7. VERIFY_SPEC**
   - Verify fix meets requirements

**8. UPDATE_SPEC**
   - Update specs if behavior changed

**9. COMMIT**
   - Commit the fix

**10. SUMMARIZE**
   - Document the bug and fix

#### Scenario: Complex bug fix
- **WHEN** a bug requires significant changes
- **THEN** follow full bugfix workflow with plan_tasks

#### Scenario: Simple bug fix
- **WHEN** a bug is small and easily fixed
- **THEN** analyze → implement → test → commit → summarize

### Requirement: Review Workflow

The review workflow SHALL follow minimal steps:

**1. ANALYZE**
   - Understand review scope
   - Identify what to review

**2. READ_SPEC**
   - Read relevant specifications

**3. VERIFY_SPEC**
   - Review code against specs
   - Categorize findings: critical / warning / suggestion

**4. SUMMARIZE**
   - Provide review report

#### Scenario: Code review
- **WHEN** user asks for a review
- **THEN** inspect the code and report findings
- **AND** categorize issues by severity

### Requirement: Small Workflow

The small workflow SHALL be used for simple changes:

**Steps:**
1. ANALYZE - Confirm it's a small change
2. IMPLEMENT - Direct code changes
3. TEST - Run tests
4. COMMIT - Commit changes
5. SUMMARIZE - Document the change

#### Scenario: Documentation update
- **WHEN** updating README or comments
- **THEN** use small workflow
- **AND** skip formal proposal/design

#### Scenario: Quick fix
- **WHEN** a one-line fix is needed
- **THEN** use small workflow for efficiency

### Requirement: Adaptive Formality

The system SHALL automatically determine formality based on change contents:

- **Large**: Has proposal + design + multiple tasks
- **Medium**: Has proposal + tasks (no design)
- **Small**: No proposal/design, ≤3 tasks

The analyze step SHALL determine the appropriate level and select steps accordingly.

#### Scenario: Large change detection
- **WHEN** analysis indicates complex changes needed
- **THEN** formality is "large" with full workflow

#### Scenario: Small change detection
- **WHEN** analysis indicates trivial changes
- **THEN** formality is "small" with minimal workflow

### Requirement: Spec Guardrails

The system SHALL enforce guardrails that protect spec integrity during implementation.

**Guardrail Rules:**
1. **MUST NOT delete** an existing spec requirement without explicit human approval
2. **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
3. **MUST NOT modify** the scenarios of a requirement being implemented

**Permitted Actions:**
- **CAN ADD** new requirements
- **CAN MODIFY** requirements not being implemented (with change proposal)
- **CAN MARK** requirements as deprecated with human approval

**Enforcement:**
- verify_spec step checks for spec drift
- update_spec step validates changes are appropriate

#### Scenario: Attempt to delete requirement
- **WHEN** an agent removes a SHALL requirement from a spec during implementation
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to weaken requirement
- **WHEN** an agent changes "SHALL" to "SHOULD"
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Workflow Entry Point

All workflows SHALL be accessed through the unified `se3 run` command.

**Entry Patterns:**
```bash
# Feature workflow
se3 run "Implement user authentication"

# Bugfix workflow  
se3 run "Fix memory leak in cache" --type=bugfix

# Review workflow
se3 run "Review the auth module" --type=review

# Small workflow
se3 run "Fix typo in README" --type=small
```

The analyze step SHALL auto-detect task type if not specified, but explicit type SHALL override.

### Requirement: Step Retry and Recovery

The system SHALL handle step failures with retry and recovery options.

**Retry Policy:**
- Automatic retry up to 3 times
- After max retries, ask user: retry / skip / abort
- User can skip failed step and continue

**Recovery:**
- State is persisted after each step
- Interrupted flows can be resumed with `se3 run --resume`
- Ctrl+C allows prompt injection before retry

#### Scenario: Step failure
- **WHEN** a step fails after 3 retries
- **THEN** user is prompted to retry, skip, or abort

#### Scenario: Flow interruption
- **WHEN** flow is interrupted mid-step
- **THEN** state is saved automatically
- **AND** `se3 run --resume` continues from interruption point
