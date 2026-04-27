<!-- spec-format: v1 -->

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

#### Scenario: Mandatory guardrails after every `se3 merge` commit
- **GIVEN** a `se3 merge <branch>` invocation produces a merge commit that touches one or more `se3/specs/**/spec.md` files (whether or not those files had textual conflicts)
- **WHEN** the merge product is evaluated
- **THEN** `se3 guardrails` is run against each touched spec file before the merge is considered complete
- **AND** the check is enforced in all three strategy tiers — `default`, `strict`, AND `fast` — so that the `fast` tier's relaxation for ordinary text conflicts does NOT extend to spec files
- **AND** any violation (deleted requirement, weakened language SHALL→SHOULD, weakened quantifier all→some, deleted scenarios) causes the merge commit to be rolled back and escalated to a human MCP call file under `se3/calls/`

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

### Requirement: New Spec vs Append Criteria

Before adding a new Requirement to an existing spec, the agent SHALL explicitly evaluate whether the content should instead become a new spec. This decision is made during the `update_spec` step and SHALL be recorded in a structured `spec_decisions` output.

**Four evaluation criteria (ALL must be met to append; if ANY fails, create a new spec):**

1. **Conceptual Independence** — The new content shares the same conceptual domain as the existing spec. It is about the same subsystem, mechanism, or abstraction level. If the content introduces a fundamentally different concept (e.g., "how to format JSON" into a spec about "error handling patterns"), it fails this test.

2. **Dependency Direction** — The new content does NOT cause existing Requirements in the spec to depend on it. If adding the Requirement would force older Requirements to reference or assume the new behavior (e.g., an existing "Retry Logic" Requirement now needs to know about a new "Circuit Breaker" Requirement), the dependency direction is wrong and a new spec is needed.

3. **Naming Test** — The new Requirement can be naturally named under the existing spec's title. A reader encountering the Requirement name should not be surprised to find it in this spec. If the name feels like it belongs in a different category, it fails this test.

4. **Cross-Scenario Reusability** — The new content is NOT expected to be referenced by multiple unrelated capabilities. If the content is a cross-cutting concern (e.g., "Authentication", "Configuration Format", "Versioning Rules") that multiple specs will need to cite, it should be its own spec to avoid circular references and provide a single source of truth.

**Decision rule:**
- If ALL four criteria pass → **append** the new Requirement to the existing spec.
- If ANY criterion fails → **create a new spec** at `se3/specs/<new_name>/spec.md` with standard structure (Purpose, Requirements, Scenarios).

**Enforcement:**
- The `update_spec` step prompt SHALL include these four criteria explicitly.
- The LLM SHALL output a `spec_decisions` array where each entry documents the decision for every new Requirement.
- The default spec loading mode for `update_spec` is `full_spec` so that the LLM can see all existing spec names and avoid naming collisions.

#### Scenario: Typical append — related requirement in same domain
- **GIVEN** the `flow-engine` spec already contains Requirements about step execution and state transitions
- **WHEN** a new Requirement about "step retry backoff strategy" is proposed
- **THEN** all four criteria pass:
  - Conceptual Independence: same domain (flow engine mechanics)
  - Dependency Direction: existing steps do not need to reference backoff
  - Naming Test: "Step Retry Backoff Strategy" fits naturally in flow-engine
  - Cross-Scenario Reusability: only flow-engine references it
- **AND** the decision is **append** to flow-engine

#### Scenario: Typical new spec — conceptually independent subsystem
- **GIVEN** the project has specs for `flow-engine`, `se3-config`, and `spec-guardrails`
- **WHEN** implementing a new "Issue Discovery" subsystem with its own data model, lifecycle, and UI
- **THEN** Criteria 1 fails (different concept from all existing specs)
- **AND** Criteria 4 fails (multiple other specs will reference issue-discovery rules)
- **AND** the decision is **new spec** — create `se3/specs/issue-discovery/spec.md`

#### Scenario: Boundary case — naming test fails but others pass
- **GIVEN** the `se3-config` spec governs YAML configuration file semantics
- **WHEN** a new Requirement about "CLI color theme configuration" is proposed
- **THEN** Criteria 1 passes (both are about configuration)
- **AND** Criteria 2 passes (existing config Requirements do not depend on color themes)
- **AND** Criteria 4 passes (only se3-config consumers care)
- **BUT** Criteria 3 fails — "CLI Color Theme Configuration" is surprising under a spec titled "se3-config" which is about framework configuration, not UI appearance
- **AND** the decision is **new spec** — create `se3/specs/ui-customization/spec.md` (or equivalent)
