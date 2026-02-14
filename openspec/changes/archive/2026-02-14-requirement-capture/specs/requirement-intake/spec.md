# requirement-intake Specification

## Purpose
Define the structured mechanism for capturing new requirements from three distinct sources: autonomous agent discovery, human-MCP interaction, and human-initiated input. This ensures all requirements flow through a unified intake process regardless of origin.

## ADDED Requirements

### Requirement: Three-Source Requirement Intake
The system SHALL accept new requirements from three distinct sources and route them through a unified `openspec change` creation process.

**Source 1: Autonomous Discovery (Agent-Initiated)**
- Trigger: Agent identifies missing capability during implementation
- Flow: Agent creates new openspec change directly → proposal captures the discovered requirement
- Marker: `[Source: autonomous-discovery]` in proposal

**Source 2: Human-MCP Call (Requested)**
- Trigger: Agent issues human call asking for requirements input
- Flow: Human responds → Agent parses response → Creates openspec change
- Marker: `[Source: human-mcp]` in proposal

**Source 3: Human-Initiated (External)**
- Trigger: Human provides new requirement through any interaction (at natural conversation boundary or during active session)
- Flow: Agent recognizes human-initiated input → Creates new openspec change
- Marker: `[Source: human-initiated]` in proposal
- Note: "Human-initiated" is about **who originated the requirement** (human, not agent), not **how it was delivered** (interrupt or natural turn). All human-proposed requirements go through this path.

#### Scenario: Autonomous discovery during implementation
- **WHEN** an agent realizes a needed feature wasn't in the original spec during coding
- **THEN** the agent creates a new openspec change with `[Source: autonomous-discovery]` marker

#### Scenario: Human provides requirements via MCP response
- **WHEN** a human fills in the Response section of a requirements-request call
- **THEN** the agent parses it and creates an openspec change with `[Source: human-mcp]` marker

#### Scenario: Human provides requirement through any interaction
- **WHEN** a human provides a new requirement (whether at natural conversation boundary or by interrupting active session)
- **THEN** the agent recognizes this as human-initiated input and creates an openspec change with `[Source: human-initiated]` marker
- **NOTE** The distinction between "interrupt" and "natural turn" is not functionally significant; both are human-initiated requirements

### Requirement: Unified Change Creation
All three intake sources SHALL result in the same downstream process: an openspec change with proposal → specs → implementation.

#### Scenario: Consistent processing regardless of source
- **WHEN** any of the three sources triggers
- **THEN** the resulting openspec change follows the same SDD workflow (proposal → specs → design → tasks → code → verify → archive)

### Requirement: Interruption Context Preservation
When Source 3 (human interrupt) occurs, the system SHALL preserve context of interrupted work.

**Context preservation options:**
1. **Stack-based**: Push current change to stack, create new interrupt change, pop on completion
2. **Link-based**: New change references "interrupted from: <change-id>"
3. **Checkpoint-based**: Mark checkpoint in current change, resume from there after interrupt

#### Scenario: Resume after interrupt
- **WHEN** a human-interrupt change completes
- **THEN** agent asks: "Resume previous work on `<change-id>`? [Y/n]"

## Usage Examples

### Example 1: Autonomous Discovery

```markdown
# New Requirement Intake

## Source: autonomous-discovery
## Timestamp: 2026-02-14T15:30:00Z
## Original Change: feature-x

# Proposal

## Why
While implementing feature-x, I discovered we need a caching layer for performance. The current approach causes O(n²) lookups.

## What Changes
- Add caching layer abstraction
- Implement in-memory cache with TTL
- Add cache invalidation hooks

## Capabilities
### New Capabilities
- `cache-layer`: Generic caching abstraction
```

### Example 2: Human-MCP Response

```markdown
# New Requirement Intake

## Source: human-mcp
## Timestamp: 2026-02-14T16:00:00Z
## Human Call: 2026-02-14-160000-api-design.md

# Proposal

## Why
Human responded to API design call: "We need rate limiting to prevent abuse."

## What Changes
- Add rate limiting middleware
- Implement token bucket algorithm
- Add rate limit headers in responses

## Capabilities
### New Capabilities
- `rate-limiting`: Request rate limiting system
```

### Example 3: Human-Initiated (Natural Turn)

```markdown
# New Requirement Intake

## Source: human-initiated
## Timestamp: 2026-02-14T17:30:00Z
## Context: Post-completion of toolize-se3

# Proposal

## Why
Human provided new requirement after previous change completed: "We should add usage analytics to track which tools are most used."

## What Changes
- Add analytics collection module
- Implement daily/weekly usage reports
- Add opt-out mechanism

## Capabilities
### New Capabilities
- `usage-analytics`: Tool usage tracking and reporting
```

### Example 4: Human-Initiated (True Interrupt)

```markdown
# New Requirement Intake

## Source: human-initiated
## Timestamp: 2026-02-14T18:00:00Z
## Interrupted: session-2026-02-14-toolize-se3
## Context: During verification phase

# Proposal

## Why
Human interrupted during verification: "Stop! We need to add audit logging before this goes live."

## What Changes
- Add audit log module
- Log all tool executions with timestamp and user
- Implement log rotation

## Capabilities
### New Capabilities
- `audit-logging`: Operation audit trail system

## Note
This interrupts the verification of toolize-se3. After completing this audit-logging change, we should resume toolize-se3 verification.
```
