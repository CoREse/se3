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
