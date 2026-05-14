<!-- spec-format: v1 -->

# SE3 Spec Format Specification

## Purpose

Define the authoritative syntax for SE3 specification files (format v1). This spec governs how Requirements are structured, named, tagged, and referenced so that programmatic tooling can parse, index, and load individual items reliably.

## Definitions

- **Spec**: A markdown file containing project specifications, located at `se3/specs/<name>/spec.md`.
- **Item**: An individual Requirement within a spec, identified by a stable name.
- **Shared sections**: Sections at the top of a spec file (before the first Requirement) that provide context for all Requirements in that spec.
- **v1 marker**: A machine-readable declaration at the top of a spec file indicating it conforms to format v1.

## Constraints

- Requirement names MUST be unique within a single spec file.
- The v1 marker, when present, MUST be the first non-whitespace content in the file.
- Tags and keywords are optional but, when present, MUST follow the syntax defined below.

## Requirements

### Requirement: Spec Format Version

A spec file MAY declare its format version via a v1 marker. The marker is an HTML comment placed at the very beginning of the file.

**Marker syntax:**
```
<!-- spec-format: v1 -->
```

- The marker MUST be the first non-whitespace line if present.
- A spec without a v1 marker is parsed in "lenient mode": the parser processes it using v1 rules but emits a warning that the format version is undeclared.
- Future format versions (v2, v3, etc.) will use the same marker pattern with an updated version string.

#### Scenario: Valid v1 marker at file start
- **GIVEN** a spec file whose first line is `<!-- spec-format: v1 -->`
- **WHEN** the parser reads the file
- **THEN** it recognizes the spec as format v1
- **AND** no version warning is emitted

#### Scenario: Missing v1 marker
- **GIVEN** a spec file with no v1 marker
- **WHEN** the parser reads the file
- **THEN** it parses the file in lenient mode
- **AND** a warning is emitted: "Spec does not declare a format version"

### Requirement: Requirement Boundary

Each Requirement is a self-contained item bounded by a heading line.

**Boundary syntax:**
```markdown
### Requirement: <name>
```

- The heading level MUST be exactly `###` (three hash marks).
- The prefix `Requirement: ` (including the colon and trailing space) is mandatory.
- The `<name>` is the item identifier and MUST be unique within the spec.
- A Requirement block extends from its `### Requirement:` heading up to (but not including) the next `### Requirement:` heading, the next `## ` heading (any second-level heading), or EOF.
- The text between the file start and the first `### Requirement:` heading constitutes the **shared header** (Purpose, Definitions, Constraints, etc.).

#### Scenario: Parsing a spec with two Requirements
- **GIVEN** a spec containing `### Requirement: Foo` followed by content, then `### Requirement: Bar`
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.requirements` has length 2
- **AND** the first Requirement's name is "Foo" and the second's is "Bar"

#### Scenario: Shared header excludes Requirement content
- **GIVEN** a spec with a Purpose section followed by `### Requirement: Foo`
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.header_text` contains the Purpose section
- **AND** `ParsedSpec.header_text` does NOT contain the Foo Requirement body

### Requirement: Shared Sections

Shared sections appear at the top of a spec file, before the first Requirement. They provide context that applies to all Requirements in the spec.

**Recognized shared section headings:**
- `## Purpose`
- `## Definitions`
- `## Constraints`
- Any other `## ` heading that appears before the first `### Requirement:`

- When an item is selected for loading, its spec's shared sections are loaded alongside it.
- The shared header text is everything from the file start up to (but not including) the first `### Requirement:` line.

#### Scenario: Shared sections loaded with selected item
- **GIVEN** a spec with `## Purpose`, `## Definitions`, and `### Requirement: Foo`
- **WHEN** the loader selects the "Foo" item
- **THEN** the output includes the Purpose and Definitions sections
- **AND** the output includes the "Foo" Requirement body

### Requirement: Tags and Keywords

Each Requirement MAY declare tags and keywords to improve discoverability and selector matching.

**Syntax within a Requirement block:**
```markdown
**tags**: authentication, security, api
**keywords**: authn, OAuth, JWT
```

- Tags and keywords are declared on lines starting with `**tags**:` or `**keywords**:` (case-insensitive) within a Requirement block.
- Values are comma-separated. Whitespace around commas is optional and is trimmed.
- Both tags and keywords are optional. A missing section yields an empty list.
- Tags are typically broad categorical labels; keywords are specific technical terms.

#### Scenario: Tags and keywords extracted from Requirement
- **GIVEN** a Requirement block containing `**tags**: auth, security` and `**keywords**: OAuth2, bearer token`
- **WHEN** the parser extracts metadata
- **THEN** `Requirement.tags` equals `["auth", "security"]`
- **AND** `Requirement.keywords` equals `["OAuth2", "bearer token"]`

#### Scenario: Missing tags and keywords default to empty list
- **GIVEN** a Requirement block with no `**tags**:` or `**keywords**:` lines
- **WHEN** the parser extracts metadata
- **THEN** `Requirement.tags` equals `[]`
- **AND** `Requirement.keywords` equals `[]`

### Requirement: Cross-Item References

Requirements MAY reference other Requirements using explicit literal references. The loader expands referenced items up to 1 hop to provide necessary context without transitive loading.

**Reference syntax (two forms):**
1. Intra-spec reference: `Requirement: <name>` — references another Requirement in the same spec.
2. Inter-spec reference: `<spec>::<requirement>` — references a Requirement in another spec.

- References are discovered by scanning the Requirement body for the two patterns above.
- The loader expands cross-item references by 1 hop: if item A references item B, loading A also loads B.
- Expansion does NOT continue beyond 1 hop (no transitive loading).
- Unresolved references (target not found) are recorded but do not cause parse errors.

**Reference name limits:**
- Reference names are truncated at 8 words to prevent runaway capture inside prose.
- Truncation stops at the first common English stop-word (e.g. `for`, `and`, `when`, `the`, `is`, `to`).
- For example, a prose sentence like "see Requirement: Foo Bar for details" extracts the reference name "Foo Bar" (stopped at `for`).
- Requirement titles longer than 8 words that are referenced in prose may be silently truncated; authors SHOULD keep Requirement names concise or use the inter-spec `spec::requirement` form for disambiguation.

#### Scenario: Intra-spec reference detected
- **GIVEN** a Requirement body containing "See also Requirement: Guardrail Enforcement"
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` contains `"Guardrail Enforcement"`

#### Scenario: Inter-spec reference detected
- **GIVEN** a Requirement body containing "As defined in flow-engine::State Machine"
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` contains `"flow-engine::State Machine"`

#### Scenario: One-hop expansion stops at boundary
- **GIVEN** item A references item B, and item B references item C
- **WHEN** the loader selects item A with max_hops=1
- **THEN** the output includes A and B
- **AND** the output does NOT include C

### Requirement: Structural Validation Contract

The spec-format package SHALL expose a `validate_spec_structure(content: str, spec_name: str) -> ValidationResult` function that decides whether a candidate `spec.md` body conforms to format v1 well enough to be safely written to disk and consumed by the rest of the system. The function is the single source of truth shared by `sync_discovery` (rejecting LLM-generated meta summaries when creating new specs), `sync_engine` (verifying disk content after a sub-agent edit or full rewrite), and the `se3 sync --validate-only` CLI audit.

**Result contract:**

1. The function SHALL return a structured `ValidationResult` (a dataclass-like record) carrying a boolean `passed` flag and an `errors` list of human-readable strings naming each failed check.
2. The function MUST be a pure function over `(content, spec_name)` — no filesystem reads, no network, no LLM, no global state — so it is cheap to call from any layer and deterministic across processes.
3. The function MUST stay at the stdlib layer (no third-party dependencies) so importing it is free for the discovery, engine, and CLI layers.

**Structural checks (all five MUST be enforced; failing any one causes `passed=False` with that check's error appended):**

1. **v1 marker** — the first non-whitespace line MUST be exactly `<!-- spec-format: v1 -->`. A spec missing this marker fails the structural contract even though the lenient-mode parser still accepts it for read-only purposes; write-back paths MUST refuse to persist such content.
2. **Specification title** — after the v1 marker, the body MUST contain a top-level heading of the form `# <spec_name> Specification`. The `<spec_name>` token MUST match the directory name (case-sensitive, kebab-case) so a file written into `se3/specs/foo/spec.md` carries `# foo Specification`.
3. **Purpose section** — the body MUST contain a `## Purpose` second-level heading. The check is a heading-level match, not a content check; an empty Purpose body still fails downstream callers' own heuristics but does not fail this validator.
4. **At least one Requirement** — the body MUST contain at least one `### Requirement: <name>` heading. A spec with shared sections only and zero Requirements is rejected because it cannot contribute any item to the loader.
5. **Non-narrative first line** — the first non-comment, non-whitespace line after the v1 marker MUST NOT begin with a narrative-prose prefix. Rejected prefixes (case-insensitive) include `I `, `I'`, `Created`, `Here`, `Let me`, `The spec`, and common Chinese equivalents (e.g., `我`, `这个`, `下面`). This catches sub-agent meta-summary outputs such as `"I have enough context from the source code and usage sites to write the spec. Let me produce it now."` that would otherwise pass the previous "length ≥ 50 chars" heuristic.

**Caller obligations:**

- `sync_discovery` SHALL call this function before writing a newly generated spec to disk. On failure the file is NOT created and the error list is surfaced to the round report.
- `sync_engine` SHALL call this function after every sub-agent invocation that may have changed a spec — both the Way A (in-place `Edit`) and Way B (full-rewrite markdown) paths. On failure the engine SHALL restore the file via `git checkout HEAD -- <spec-path>` (Way A) or refuse the write (Way B) and SHALL NOT refresh the in-memory cache from invalid content.
- `se3 sync --validate-only` SHALL run this function against every `se3/specs/**/spec.md` and report each failure with the specific error strings, exiting `1` if any spec fails and `0` otherwise.

#### Scenario: Valid spec passes structural validation
- **GIVEN** a spec body whose first non-whitespace line is `<!-- spec-format: v1 -->`, followed by `# my-feature Specification`, `## Purpose`, and at least one `### Requirement: ...` heading, with no narrative prefix on the first content line
- **WHEN** `validate_spec_structure(content, "my-feature")` is called
- **THEN** the returned result has `passed=True` and `errors == []`

#### Scenario: Missing v1 marker fails
- **GIVEN** a spec body that does not begin with `<!-- spec-format: v1 -->`
- **WHEN** `validate_spec_structure(content, name)` is called
- **THEN** `passed=False` and `errors` contains an entry naming the missing v1 marker

#### Scenario: Sub-agent meta summary is rejected as a narrative first line
- **GIVEN** a candidate body whose first non-comment line is `"I have enough context from the source code and usage sites to write the spec. Let me produce it now."`
- **WHEN** the validator runs
- **THEN** `passed=False` and `errors` includes a narrative-first-line entry
- **AND** the caller (e.g., `sync_discovery`) does NOT write the file to disk

#### Scenario: Spec with zero Requirements fails
- **GIVEN** a body with a valid v1 marker, Specification title, and Purpose section, but no `### Requirement:` heading
- **WHEN** the validator runs
- **THEN** `passed=False` and `errors` includes a missing-Requirement entry

#### Scenario: Spec name mismatch in Specification title fails
- **GIVEN** the file is being written into `se3/specs/foo/spec.md` (so `spec_name == "foo"`)
- **AND** the body's top-level heading reads `# bar Specification`
- **WHEN** `validate_spec_structure(content, "foo")` is called
- **THEN** `passed=False` and `errors` names the title/name mismatch

#### Scenario: --validate-only CLI surfaces every failure
- **GIVEN** the user runs `se3 sync --validate-only` against a project that contains one spec missing its v1 marker and one spec whose first line is a narrative prefix
- **WHEN** the command executes
- **THEN** both specs are listed in the failure report with their specific errors
- **AND** the command exits with status `1`
