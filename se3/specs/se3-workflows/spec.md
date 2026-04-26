<!-- spec-format: v1 -->
# se3-workflows Specification

## Purpose

Define the standard workflows for SE3 development using the Flow Engine's 13-step state machine. These workflows govern how different types of work are processed from intake to completion through the unified `se3 run` entry point.

## Requirements

### Requirement: Workflow Types

The system SHALL support five workflow types, mapped to different step sequences from the step pool:

| Type | Steps | When Used |
|------|-------|-----------|
| `feature` | analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit | New functionality or significant enhancement |
| `bugfix` | analyze → plan → implement → test → self_check → verify_spec → version_analyze → commit | Bug reports (plan uses medium depth) |
| `review` | analyze → verify_spec | Code review, audit, or analysis |
| `small` | analyze → implement → test → version_analyze → commit | Minor fixes, typos, simple changes |
| `directive` | analyze → plan → implement → version_analyze → commit | Following specific instructions (plan uses shallow depth) |

**Step Pool (9 active steps in default sequences):**
1. **analyze** - Analyze task type and scope, collect project context, select and load relevant specs
2. **plan** - Unified planning: proposal + design + task breakdown (adapts depth by task_type)
3. **implement** - Write code implementation
4. **test** - Run tests to verify
5. **self_check** - LLM code review for logic completeness, robustness, functional gaps, and test coverage gaps (excludes spec compliance)
6. **verify_spec** - Check implementation vs spec
7. **update_spec** - Update spec records
8. **version_analyze** - Analyze changes to determine SemVer bump type and generate commit message
9. **commit** - Commit changes (generates template summary when summarize step is absent)

**Optional step (available in pool, not in default sequences):**
- **summarize** - Generate LLM-based summary and handoff. Can be added to step sequences via `se3.yaml` configuration. When absent, the commit step generates a template-based summary document.

**Note:** `read_spec` and `project_summary` are deprecated — their functionality is now merged into the `analyze` step. Deprecated handlers are retained for backward compatibility with persisted flows.

#### Scenario: Feature workflow selection
- **WHEN** input is classified as "feature-request"
- **THEN** the system uses the feature workflow with full 9 default steps

#### Scenario: Bug fix workflow selection
- **WHEN** input is classified as "bug-report"
- **THEN** the system uses the bugfix workflow (plan uses medium depth)

#### Scenario: Small workflow selection
- **WHEN** the change is simple (no spec changes needed, ≤3 tasks)
- **THEN** the system uses the small workflow for efficiency

### Requirement: Feature Workflow

The feature workflow SHALL follow these steps:

**1. ANALYZE**
   - Collect structured project context via `ProjectContextCollector.collect()` (programmatic, no LLM)
   - Programmatically list available spec names
   - Single LLM call to classify task type (feature/bugfix/review/small/directive), determine scope and complexity, and select relevant specs (`selected_specs`)
   - Post-processing: programmatically load spec content (base spec auto-attached + selected specs)
   - Outputs: task_type, scope, complexity, reasoning, project_summary, relevant_specs, spec_content, selected_specs

**2. PLAN** (unified planning step, adapts depth by task_type)
   - Generate change proposal (summary, motivation, files, risks)
   - Create design document (architecture decisions, components, data flow)
   - Break implementation into concrete task groups
   - Estimate complexity and lines of code (`estimated_loc`) for each task
   - All produced in a single LLM call with adaptive prompt depth:
     - feature/discovery: full depth (proposal + design + tasks)
     - bugfix: medium depth (proposal + lightweight design + tasks)
     - directive/small: shallow depth (tasks only)

**3. IMPLEMENT**
   - Display structured task plan panel showing execution strategy, task groups with LOC estimates, and LOC summary before any LLM calls
   - Write code following the plan
   - If total estimated LOC ≤ threshold (default 300), collapse all groups into a single LLM call
   - If total estimated LOC > threshold, execute groups via DAG parallel with branch relay strategy
   - Include tests where applicable
   - Follow project conventions

**4. TEST**
   - Run test suite automatically
   - Report test results
   - If tests fail, trigger fix loop to return to implement step
   - If tests pass, continue to self_check for code review

**5. SELF_CHECK**
   - LLM reviews implementation for logic completeness, robustness, functional gaps, and test coverage gaps
   - Explicitly excludes spec compliance checks (handled by verify_spec)
   - Receives test_results and changes_made as input context
   - If critical/high issues found, trigger fix loop to return to implement step
   - If no critical/high issues, continue to verify_spec

**6. VERIFY_SPEC**
   - Check implementation against specifications
   - Verify all scenarios are covered
   - Identify any discrepancies

**7. UPDATE_SPEC**
   - Update specs to reflect changes made
   - Add new capabilities documentation
   - Mark scenarios as implemented

**8. VERSION_ANALYZE**
   - Analyze changes to determine SemVer bump type
   - Generate commit message

**9. COMMIT**
   - Stage and commit all changes
   - Use commit message from version_analyze (or fallback chain)
   - Update version according to bump rules
   - Generate template summary document when summarize step is absent

#### Scenario: Large feature
- **WHEN** a feature is complex with multiple components
- **THEN** go through all 9 default steps with full-depth plan
- **AND** the plan includes formal proposal, design, and task groups

#### Scenario: Medium feature
- **WHEN** a feature is moderately complex
- **THEN** the plan step adapts depth automatically

### Requirement: Bug Fix Workflow

The bugfix workflow SHALL follow these steps (plan uses medium depth):

**1. ANALYZE**
   - Reproduce the bug
   - Identify root cause
   - Determine affected components
   - Collect project context and load relevant specs (merged from former project_summary and read_spec steps)

**2. PLAN** (medium depth: proposal + lightweight design + tasks)
   - Generate fix proposal
   - Identify files to modify
   - Break complex fixes into task groups

**3. IMPLEMENT**
   - Fix the bug
   - Add regression tests

**4. TEST**
   - Run tests to verify fix
   - Run regression tests

**5. SELF_CHECK**
   - LLM reviews fix for logic completeness, robustness, and functional gaps
   - Ensures the fix doesn't introduce new issues or miss related changes

**6. VERIFY_SPEC**
   - Verify fix meets requirements

**7. VERSION_ANALYZE**
   - Determine version bump type and generate commit message

**8. COMMIT**
   - Commit the fix with version bump

#### Scenario: Complex bug fix
- **WHEN** a bug requires significant changes
- **THEN** follow full bugfix workflow with plan step

#### Scenario: Simple bug fix
- **WHEN** a bug is small and easily fixed
- **THEN** analyze → implement → test → commit → summarize

### Requirement: Review Workflow

The review workflow SHALL follow minimal steps:

**1. ANALYZE**
   - Understand review scope
   - Identify what to review
   - Collect project context and load relevant specs

**2. VERIFY_SPEC**
   - Review code against specs (consumes `spec_content` directly from analyze)
   - Categorize findings by priority: critical / high / medium / low
   - Classify scope: in_scope / out_of_scope

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
4. VERSION_ANALYZE - Determine version bump and generate commit message
5. COMMIT - Commit changes

#### Scenario: Documentation update
- **WHEN** updating README or comments
- **THEN** use small workflow
- **AND** skip the plan step entirely

#### Scenario: Quick fix
- **WHEN** a one-line fix is needed
- **THEN** use small workflow for efficiency

### Requirement: Adaptive Formality

The system SHALL automatically determine formality based on change contents:

- **Large**: Full-depth plan with proposal + design + multiple task groups
- **Medium**: Medium-depth plan with proposal + tasks
- **Small**: No plan step, ≤3 tasks

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

#### Scenario: Entry with explicit type
- **GIVEN** user wants to run a specific workflow type
- **WHEN** user executes `se3 run "task" --type=bugfix`
- **THEN** the system uses the bugfix workflow
- **AND** uses the bugfix workflow with medium-depth plan

#### Scenario: Entry with auto-detection
- **GIVEN** user provides a task description
- **WHEN** user executes `se3 run "Implement new feature"`
- **THEN** the analyze step auto-detects the task type
- **AND** selects appropriate workflow

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
