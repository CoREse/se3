<!-- spec-format: v1 -->
# spec-role Specification

## Purpose

Define se3's **code-first / spec-assistant** governance model as a single source of truth, and the mechanism that keeps it from regressing. se3 treats the project source code as primary and authoritative; a spec under `se3/specs/**/spec.md` is the *documented snapshot* of what the code currently does — a read-only reference for humans and agents, not a design the code must be bent to satisfy. This spec owns the canonical wording of that role, the rule that prompts reference it instead of improvising their own framing, and the anti-regression guardrail that blocks "spec-driven / spec-overrides-code" framing from creeping back into prompts and documentation.

The within-flow drift-guard half of the governance model (the `spec → code` direction enforced by `se3 guardrails` and `verify_spec`) is detailed in the **spec-guardrails** and **flow-engine** specs; this spec defines the overall asymmetric model those mechanisms operate under.

## Requirements

### Requirement: Code-First Spec Role Definition

The system SHALL maintain a single authoritative, machine- and human-readable statement of the code-first / spec-assistant role, exported as `SPEC_ROLE_DEFINITION` from `src/se3/engine/spec_role.py`. Step prompts and documentation SHALL reference this single wording rather than re-paraphrasing the spec's role inline.

`src/se3/engine/spec_role.py` SHALL have no import-time side effects and depend only on the standard library, so both prompt modules and tests can import it freely.

**The definition SHALL state the asymmetric code↔spec governance model:**

1. **code → spec is the primary direction.** `se3 sync` regenerates the spec from the current code so the spec keeps reflecting reality. When the spec and the code disagree, the code wins and the spec is updated — never the reverse.
2. **spec → code is only a bounded, within-flow drift guard.** For the duration of a single flow, `se3 guardrails` treats the already-recorded SHALL/MUST requirements as the implementation contract *for that flow*, so an in-progress implementation cannot silently weaken or delete requirements mid-flow. This is drift prevention scoped to one flow; it does NOT make the spec authoritative over the code in general.

**The definition SHALL also state the consequences for how an agent works:**

- Any "Available Specifications" provided to a step are a read-only reference to the code's current state, not architectural contracts the code must be aligned to.
- Future intent and desired changes enter through issues (`se3 issue`), not by rewriting specs to describe a not-yet-built future.
- Agents do not proactively propose creating or rewriting spec files. Recording code into specs is the job of the `update_spec` step and `se3 sync`, not of analysis/discovery work.

**Where content lands, and who restructures the spec corpus (spec-volume governance roles):**

- The `base` spec carries only project-top-level content that every session genuinely needs loaded in full (project identity, the global architecture picture, cross-cutting conventions, and a one-line locator index of each module / spec); module-specific detail belongs in the corresponding module spec, reachable on demand via `se3 spec index` / `se3 spec show`. This admission standard is stated in full in the `spec-format` *Spec Volume Governance Standards* Requirement.
- Semantic-level restructuring of the spec corpus — relocating over-admission `base` content into module specs, and splitting an over-sized multi-topic spec into parallel specs — is performed ONLY by `se3 sync` (with the plan confirmed through sync's respond channel). The `update_spec` step MUST NOT create a parallel spec on its own; when it judges a spec should be split, it only records the recommendation in its output and leaves the restructure to `se3 sync`.

The human-readable README mirrors this wording (the "Spec ↔ code two-way governance (asymmetric)" entry) so the CLI documentation and the injected prompt wording cannot drift apart.

#### Scenario: Single authoritative wording is imported, not re-paraphrased
- **WHEN** a step prompt or another module needs to state the role of specs
- **THEN** it imports and injects `SPEC_ROLE_DEFINITION` from `se3.engine.spec_role`
- **AND** it does not write its own competing paraphrase of "what a spec is for"

#### Scenario: Asymmetric governance is explicit
- **WHEN** the role definition describes code↔spec governance
- **THEN** it names `se3 sync` as the primary `code → spec` direction in which the code wins on disagreement
- **AND** it names `se3 guardrails` as a bounded, within-flow `spec → code` drift guard that does NOT make the spec authoritative over the code in general

#### Scenario: Spec-corpus restructuring is reserved to se3 sync
- **WHEN** the implementation introduces detail that belongs to a module rather than to `base`, or a spec grows large enough to warrant a split
- **THEN** `update_spec` writes module detail into the corresponding module spec (not `base`) and, for a split, only records a recommendation in its output
- **AND** the actual `base` content relocation or parallel-spec split is performed by `se3 sync`, confirmed through sync's respond channel

### Requirement: Spec Role Prompt Injection

Steps that present specs to an LLM SHALL inject the code-first / spec-assistant framing so every LLM sub-process shares one description of what a spec is for, and SHALL present "Available Specifications" as a read-only reference rather than a contract.

**Injection points:**

1. The `discovery` step prompts (both `INITIAL_DISCOVERY_PROMPT` and `CONTINUE_DISCOVERY_PROMPT`) SHALL inject `SPEC_ROLE_DEFINITION`, label the "Available Specifications" section as a read-only reference to the code's current state, and explicitly forbid proposing the creation or rewriting of spec files.
2. The `analyze` step prompt SHALL inject `SPEC_ROLE_DEFINITION` to reinforce that the specs it selects are a read-only reference, not architectural contracts the task must be aligned to, and that analyze must not propose creating or rewriting spec files.
3. The `plan` step prompt SHALL frame its "Relevant Specifications" section as a read-only reference to how the code currently behaves, not contracts the plan must be aligned to.

**Discovery / analyze behavior constraint:** Recording code into specs is the responsibility of the `update_spec` step and `se3 sync`. The `discovery` and `analyze` steps SHALL NOT proactively propose creating new spec files or rewriting existing ones; intent for future change is captured through issues, not by editing specs to describe unbuilt behavior.

#### Scenario: Discovery injects the role and forbids proposing specs
- **WHEN** the `discovery` step builds its prompt
- **THEN** the prompt contains the `SPEC_ROLE_DEFINITION` wording
- **AND** the "Available Specifications" section is described as a read-only reference to current code behavior, not a contract the task must be aligned to
- **AND** the prompt instructs the LLM not to propose creating or rewriting spec files (deferring that to `update_spec` / `se3 sync`)

#### Scenario: Analyze reinforces read-only spec reference
- **WHEN** the `analyze` step builds its prompt
- **THEN** the prompt contains the `SPEC_ROLE_DEFINITION` wording so the selected specs are treated as a read-only reference, not architectural contracts to align the task to

#### Scenario: Plan frames specs as a read-only reference
- **WHEN** the `plan` step builds its prompt
- **THEN** its "Relevant Specifications" section states the specs are a read-only reference to how the code currently behaves and that the code is authoritative

### Requirement: Anti-Regression Spec-Driven Framing Guardrail

The system SHALL maintain a curated set of phrases, exported as `SPEC_DRIVEN_FRAMING_PHRASES` from `src/se3/engine/spec_role.py`, that unambiguously express the rejected "spec drives / overrides the code" framing, plus a `find_spec_driven_framing(text)` matching helper (case-insensitive) that is the single matching helper used by the guardrail. A repository-level pytest regression test SHALL scan prompt and documentation source files for these phrases and FAIL — pinpointing `file:line` — when any occurs, so that spec-driven residuals cannot regress into the codebase.

**Curation policy:** `SPEC_DRIVEN_FRAMING_PHRASES` SHALL record ONLY phrases that unambiguously frame the spec as the thing that drives or overrides the code (e.g., `spec-driven`, `specs drive`, `drive an ai coding agent`, `must align with the spec`, `spec overrides the code`). It SHALL deliberately EXCLUDE generic or borderline tokens that have legitimate, compliant uses elsewhere in the repo, so the guardrail does not produce false positives that would force-rewrite correct text or be quietly suppressed (defeating the guardrail). In particular it MUST NOT contain:
- `contract` — used legitimately for wire-protocol contracts, parser contracts, the version-script output contract, and the within-flow temporary contract.
- `source of truth` — used legitimately for "single source of truth: pyproject.toml", "the markdown body is the single source of truth", etc.
- `two-way governance` / `Spec ↔ code two-way governance` — describes the compliant asymmetric governance model, not a spec-driven framing.

**Scan scope:** The regression test SHALL scan, at minimum: the step prompt modules under `src/se3/engine/steps/`; the spec-writing prompt modules `sync_engine.py`, `sync_discovery.py`, and `sync_analyzer.py`; the `src/se3/engine/merge/` modules; the README mirrors `README.md` and `README.zh.md`; `docs/*.md`; `src/se3/templates/*.md`; and `src/se3/engine/runtime_environment.md`. The authoritative-definition module `src/se3/engine/spec_role.py` itself SHALL be excluded from the scan, because it is the single place that legitimately stores the curated rejected-framing phrases it scans for; scanning it would be a self-match. The test SHALL also assert the scan scope is non-empty so that a moved directory cannot silently reduce coverage to nothing.

The phrase set SHALL be owned by `se3.engine.spec_role` and imported by the test (single source of the phrase set), so the prompt-injection wording and the guardrail cannot drift apart.

#### Scenario: Spec-driven phrase in scanned source fails the guardrail
- **GIVEN** a scanned prompt or documentation source file contains a curated `SPEC_DRIVEN_FRAMING_PHRASES` phrase (e.g., `spec-driven` or `drive an ai coding agent`)
- **WHEN** the regression test runs
- **THEN** the test fails and reports the offending `file:line`
- **AND** the failure points the author at the compliant `SPEC_ROLE_DEFINITION` wording

#### Scenario: Compliant borderline text is not flagged
- **GIVEN** scanned source contains compliant text such as "Spec ↔ code two-way governance", "single source of truth: pyproject.toml", or a "wire-protocol contract"
- **WHEN** the regression test runs
- **THEN** none of those phrases is flagged, because the curated set excludes generic / borderline tokens

#### Scenario: Authoritative module is excluded from the scan
- **WHEN** the regression test enumerates files to scan
- **THEN** `src/se3/engine/spec_role.py` is NOT included
- **AND** the curated phrases it stores do not trigger a self-match

#### Scenario: Scan scope is guarded against silent emptiness
- **WHEN** the scan-scope resolver runs
- **THEN** it returns a non-empty list including the README mirrors, the step prompt package, and `runtime_environment.md`
- **AND** a moved or renamed directory that emptied the scope would fail the scope assertion rather than passing vacuously
