# requirement-intake Specification

## Purpose

Define the requirement intake process for SE3, governing how new requirements enter the system through the unified `se3 run` entry point.

## Requirements

### Requirement: Requirement Intake via se3 run

The system SHALL accept new requirements through the `se3 run` command interface.

**Intake methods:**

**Method 1: Direct Task Description**
- Command: `se3 run "Implement user authentication"`
- Flow: Task description → analyze step → workflow execution
- Use for: Clear, well-defined tasks

**Method 2: Discovery Mode**
- Command: `se3 run --discover "I want to build something..."`
- Flow: Multi-turn exploration → refined description → analyze step
- Use for: Vague ideas that need clarification

**Method 3: Resume Existing Flow**
- Command: `se3 run --resume`
- Flow: Load persisted state → continue from interrupted step
- Use for: Continuing interrupted work

#### Scenario: Direct task intake
- **WHEN** user executes `se3 run "Implement feature X"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: Discovery mode intake
- **WHEN** user executes `se3 run --discover "Idea"`
- **THEN** the flow engine starts discovery step
- **AND** explores requirements through conversation
- **AND** proceeds to analyze after user confirms refined description

#### Scenario: Resume flow
- **WHEN** user executes `se3 run --resume`
- **THEN** the flow engine loads the active flow state
- **AND** continues execution from the interrupted step

### Requirement: Task Type Classification

The system SHALL classify tasks into types during the analyze step.

**Task Types:**
- `feature` - New functionality or significant enhancement
- `bugfix` - Fixing a bug or issue
- `review` - Code review, audit, or analysis
- `small` - Minor fix, typo, or simple change
- `directive` - Following specific instructions

**Classification Factors:**
- Scope of changes
- Complexity
- Need for design documentation
- Test requirements

#### Scenario: Feature classification
- **GIVEN** task description "Add user authentication system"
- **WHEN** analyze step executes
- **THEN** task type is classified as `feature`
- **AND** full 11-step workflow is selected

#### Scenario: Small change classification
- **GIVEN** task description "Fix typo in README"
- **WHEN** analyze step executes
- **THEN** task type is classified as `small`
- **AND** abbreviated workflow is selected

### Requirement: Flow State Persistence

The system SHALL persist flow state after each step for resumability.

**Persistence:**
- State stored in `se3/state/engine.json`
- Each step completion updates the state
- Flow can be resumed from any step

#### Scenario: Interrupt and resume
- **GIVEN** a flow is executing the implement step
- **WHEN** user interrupts (Ctrl+C)
- **THEN** current state is persisted
- **AND** next `se3 run --resume` continues from implement step

### Requirement: Task Source Tracking

The system MAY track the source of tasks for analytics.

**Source markers (optional):**
- `direct` - Direct `se3 run "task"` command
- `discovery` - Discovery mode refined description
- `loop` - Loop mode task

#### Scenario: Source tracking
- **WHEN** a flow completes
- **THEN** the summary MAY include the task source
