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
- Truncation stops at the first common English stop-word.
- For example, a prose sentence like "see Requirement: Foo Bar for details" extracts the reference name "Foo Bar" (stopped at `for`).
- Requirement titles longer than 8 words that are referenced in prose may be silently truncated; authors SHOULD keep Requirement names concise or use the inter-spec `spec::requirement` form for disambiguation.

**Stop-word set (case-insensitive, compared against each captured word in lowercase):**

The truncation scanner SHALL treat the following tokens as stop-words. Encountering any one of them ends the captured reference name at the preceding word, and the stop-word itself is NOT included in the extracted name:

- Prepositions and conjunctions: `for`, `and`, `or`, `but`, `not`, `of`, `in`, `on`, `at`, `by`, `with`, `from`, `as`, `to`.
- Relative/demonstrative pronouns: `that`, `this`, `it`, `the`.
- Subordinators / temporal markers: `when`.
- Common auxiliary and copular verbs: `is`, `are`, `was`, `were`, `be`, `been`, `have`, `has`, `had`, `do`, `does`, `did`.

This list is the authoritative stop-word set; the example tokens elsewhere in this Requirement (`for`, `and`, `when`, `the`, `is`, `to`) are illustrative and form a strict subset of the full list above. Implementations MUST honour every entry in the full set so reference capture stops consistently across prose patterns such as "Requirement: Foo Bar that documents…" (stops at `that`), "Requirement: Foo Bar of the system" (stops at `of`), or "Requirement: Foo Bar with caveats" (stops at `with`).

#### Scenario: Stop-word beyond the illustrative examples halts capture
- **GIVEN** a Requirement body containing the prose phrase `"See Requirement: Foo Bar with additional caveats"`
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` contains `"Foo Bar"` (capture halted at the stop-word `with`)
- **AND** `Requirement.refs` does NOT contain `"Foo Bar with"` or any longer span including `with`

#### Scenario: Auxiliary verb stop-word halts capture
- **GIVEN** a Requirement body containing the prose phrase `"per Requirement: Foo Bar has been deprecated"`
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` contains `"Foo Bar"` (capture halted at the stop-word `has`)
- **AND** subsequent tokens such as `been` and `deprecated` are NOT folded into the captured name

**Excluded contexts (matches inside these regions are NOT extracted as references):**

To prevent false-positive references arising from illustrative or tabular prose, the reference scanner SHALL strip the following regions from the Requirement body before applying the `Requirement: <name>` and `<spec>::<requirement>` patterns:

- **Fenced code blocks** — any region delimited by triple-backtick fences (``` ``` ```), including the fence lines themselves. A `Requirement: Foo` token appearing inside a code sample is NOT extracted as a reference, because such tokens are typically syntax examples or escaped illustrations rather than genuine cross-item links.
- **Markdown table rows** — any line whose content is a Markdown table row (a line containing pipe characters in the table-row shape, e.g. `| col1 | col2 |` or `|---|---|`). Tokens appearing inside a table cell are NOT extracted as references, because tables are typically used for tabular data presentation rather than narrative cross-references and would otherwise produce noisy spurious refs from cells that mention requirement-like phrases.

These exclusions are applied uniformly to both the intra-spec `Requirement: <name>` form and the inter-spec `<spec>::<requirement>` form. Tokens appearing in ordinary prose paragraphs, list items, blockquotes, or headings continue to be extracted normally.

#### Scenario: Reference inside a fenced code block is ignored
- **GIVEN** a Requirement body containing a triple-backtick fenced code block whose content includes the line `See Requirement: Foo Bar`
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` does NOT contain `"Foo Bar"`
- **AND** the same `Requirement: Foo Bar` token appearing OUTSIDE the fenced block in the same body IS extracted normally

#### Scenario: Reference inside a markdown table row is ignored
- **GIVEN** a Requirement body containing a Markdown table whose cells include text such as `| Linked item | Requirement: Foo Bar |`
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` does NOT contain `"Foo Bar"`
- **AND** the same `Requirement: Foo Bar` token appearing in a non-table prose paragraph in the same body IS extracted normally

#### Scenario: Inter-spec reference inside a fenced code block is ignored
- **GIVEN** a Requirement body whose fenced code block contains the token `flow-engine::State Machine`
- **WHEN** the parser extracts references
- **THEN** `Requirement.refs` does NOT contain `"flow-engine::State Machine"`

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

**Input-degenerate cases (short-circuit before structural checks):**

The validator never raises. Two degenerate inputs are handled before the structural checks run and short-circuit immediately to a single-error failure result:

- **Non-string input** — if `content` is not a `str`, the function returns `passed=False` with `errors == ["content is not a string"]` and no structural checks are performed.
- **Empty content** — if `content` is a string but contains no non-blank line (empty string, whitespace-only, or only blank lines), the function returns `passed=False` with `errors == ["spec is empty"]` and no structural checks are performed.

These short-circuits guarantee callers can pass arbitrary candidate buffers — including `None`-like placeholders coerced from upstream parse failures or empty sub-agent outputs — without the validator raising and without producing a misleading list of every structural rule "failing." Both cases yield a result whose `errors` list contains exactly one entry naming the degenerate condition.

**Structural checks (all five MUST be enforced; failing any one causes `passed=False` with that check's error appended):**

1. **v1 marker** — the first non-whitespace line MUST be exactly `<!-- spec-format: v1 -->`. A spec missing this marker fails the structural contract even though the lenient-mode parser still accepts it for read-only purposes; write-back paths MUST refuse to persist such content.
2. **Specification title** — after the v1 marker, the body MUST contain a top-level heading of the form `# <spec_name> Specification`. The trailing word `Specification` is required. The `<spec_name>` token is matched case-insensitively against the directory name, and the validator accepts either of the following as a match:
   - the full kebab-case directory name appears (case-insensitively) inside the heading text, OR
   - as a tolerant fallback for legacy human-authored titles, any single dash-separated token of the directory name appears (case-insensitively) inside the heading text. For example, `# SE3 Version Management Specification` validates for a spec directory named `se3-versioning` because the token `se3` appears in the title.
   New specs SHOULD use the canonical `# <spec_name> Specification` form matching the directory name in kebab-case; the token-level fallback exists to keep pre-existing prose titles valid.
3. **Purpose section** — the body MUST contain a `## Purpose` second-level heading AND that section MUST have non-empty body content beneath it. A spec missing the heading fails with a missing-section error; a spec with the heading but an empty body fails with an `'## Purpose' section is empty` error.
4. **At least one Requirement** — the body MUST contain at least one `### Requirement: <name>` heading. A spec with shared sections only and zero Requirements is rejected because it cannot contribute any item to the loader.
5. **Non-narrative first line** — the first non-comment, non-whitespace line after the v1 marker MUST NOT begin with a narrative-prose prefix. The check is performed by lowercasing that line and testing whether it `startswith` any rejected prefix. Rejected prefixes (case-insensitive) include:
   - First-person openers: `I ` (trailing space), `I'` (e.g., `I'll`, `I've`), `I,` (e.g., `I, having reviewed…`), `I.` (e.g., `I. Introduction`).
   - Self-introductory openers: `Created`, `Here ` (trailing space), `Here's`, `Here is`, `Let me`.
   - Spec-narration openers: `The spec`, `This spec`.
   - Common Chinese equivalents: `我`, `已经`, `让我`.

   This catches sub-agent meta-summary outputs such as `"I have enough context from the source code and usage sites to write the spec. Let me produce it now."` or `"Here's the updated spec."` or `"This spec describes…"` that would otherwise pass the previous "length ≥ 50 chars" heuristic. The punctuation-bearing first-person variants (`I,`, `I.`) ensure prose openers without a following space — e.g., `"I, the assistant…"` or `"I. Overview"` — are also rejected.

**Caller obligations:**

- `sync_discovery` SHALL call this function before writing a newly generated spec to disk. On failure the file is NOT created and the error list is surfaced to the round report.
- `sync_engine` SHALL call this function after every sub-agent invocation that may have changed a spec — both the Way A (in-place `Edit`) and Way B (full-rewrite markdown) paths. On failure the engine SHALL restore the file via `git checkout HEAD -- <spec-path>` (Way A) or refuse the write (Way B) and SHALL NOT refresh the in-memory cache from invalid content.
- **Way-A rollback fallback**: if the `git checkout HEAD -- <spec-path>` rollback itself fails (e.g., the spec is untracked, the working tree state prevents checkout, or git is unavailable), the engine SHALL perform a best-effort write of the pre-call disk content (captured before the sub-agent ran) back to the file, and SHALL update the in-memory cache to that restored content. This guarantees the on-disk spec is never left in an invalid post-edit state when validation rejects a Way-A result. If this fallback write also fails (e.g., due to an `OSError`), the engine logs the error and returns failure without further attempts; it MUST NOT refresh the in-memory cache from invalid content in any branch.
- **Mid-flight LLM error recovery**: if the LLM call itself raises an exception (e.g., network drop, timeout, or remote error) AFTER the sub-agent has already invoked the `Edit` tool to mutate the spec file on disk (Way A), the engine SHALL detect the partial edit by comparing pre-call and post-error disk snapshots (SHA of the spec file). When the disk content changed despite the error, the engine SHALL re-read the file and run `validate_spec_structure` on the post-error content:
  - If validation passes, the engine SHALL accept the partial edit as a successful Way-A update, refresh the in-memory cache from the new content, and return success. The original LLM exception is treated as recoverable because the on-disk result is structurally sound.
  - If validation fails, the engine SHALL roll the file back via `git checkout HEAD -- <spec-path>` and refresh the in-memory cache from the restored disk content (falling back to the captured pre-call content if the re-read fails). The engine SHALL return failure and MUST NOT cache invalid content.
  - If the post-error re-read of the spec file itself fails (`OSError`), the engine SHALL invoke the same `git checkout HEAD -- <spec-path>` rollback, restore the in-memory cache to the captured pre-call content, and return failure.
  - If the disk did not change between the pre-call and post-error snapshots, no recovery is attempted and the engine returns failure.

  This recovery path ensures a sub-agent's successful pre-error file write is not discarded when the surrounding LLM call later raises, while preserving the invariant that invalid content is never persisted or cached.
- `se3 sync --validate-only` SHALL run this function against every `se3/specs/**/spec.md` and report each failure with the specific error strings, exiting `1` if any spec fails and `0` otherwise. The CLI SHALL additionally:
  - Render per-spec results as a Rich table whose rows show each spec's name and a PASS/FAIL status cell, with PASS rendered in a success color (e.g., green) and FAIL rendered in an error color (e.g., red) so operators can visually scan the result set. The table SHALL additionally include a third `Errors` column whose cell contains, for each failing spec, the spec's specific error strings (joined into a single cell — typically newline-separated) drawn from the `ValidationResult.errors` list returned by `validate_spec_structure`; passing specs show an empty `Errors` cell. The `Errors` column lets operators read the failure reasons inline with the PASS/FAIL summary instead of cross-referencing a separate report block.
  - Skip any directory under `se3/specs/` whose name begins with `_` or `.` as a framework-internal or hidden directory. Such directories MUST NOT be loaded, validated, or counted toward the exit-status decision; their presence on disk has no effect on the command's outcome.
  - Surface **directory-level errors** as failure rows in the same Rich table, alongside structural-validation failures, so operators see every spec-directory problem in a single report:
    - If a non-skipped spec directory contains no `spec.md` file, the command SHALL add a FAIL row for that directory whose `Errors` cell contains the single string `"spec.md missing"`. The `validate_spec_structure` function is NOT called for such directories (there is no candidate content to validate).
    - If reading a directory's `spec.md` raises an `OSError` (e.g., permission denied, I/O error), the command SHALL add a FAIL row for that directory whose `Errors` cell contains a single string of the form `"read error: <exc>"`, where `<exc>` is the stringified exception. The `validate_spec_structure` function is NOT called when the file cannot be read.
    - Both directory-level failure modes count toward the failure tally and cause the command to exit with status `1`, identical to structural-validation failures.

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

#### Scenario: Non-string input short-circuits to a single error
- **GIVEN** a caller invokes `validate_spec_structure(content, name)` where `content` is not a `str` (e.g., `None`, bytes, or any non-string value)
- **WHEN** the validator runs
- **THEN** `passed=False` and `errors` equals `["content is not a string"]`
- **AND** no structural rule errors are appended

#### Scenario: Empty content short-circuits to a single error
- **GIVEN** a caller invokes `validate_spec_structure(content, name)` where `content` is a string containing no non-blank line (empty, whitespace-only, or only blank lines)
- **WHEN** the validator runs
- **THEN** `passed=False` and `errors` equals `["spec is empty"]`
- **AND** no structural rule errors are appended

#### Scenario: --validate-only CLI surfaces every failure
- **GIVEN** the user runs `se3 sync --validate-only` against a project that contains one spec missing its v1 marker and one spec whose first line is a narrative prefix
- **WHEN** the command executes
- **THEN** both specs are listed in the failure report with their specific errors
- **AND** the command exits with status `1`

#### Scenario: --validate-only CLI renders per-spec results in a Rich table
- **GIVEN** the user runs `se3 sync --validate-only` against a project with multiple specs, some passing and some failing
- **WHEN** the command executes
- **THEN** the output includes a Rich table with one row per validated spec
- **AND** passing specs have a PASS status cell rendered in a success color
- **AND** failing specs have a FAIL status cell rendered in an error color

#### Scenario: --validate-only Rich table includes an Errors column with per-spec error strings
- **GIVEN** the user runs `se3 sync --validate-only` against a project containing a passing spec and a failing spec whose `validate_spec_structure` result has `errors == ["missing v1 marker", "'## Purpose' section is empty"]`
- **WHEN** the command executes
- **THEN** the Rich table includes a third `Errors` column alongside the Spec and Status columns
- **AND** the failing spec's `Errors` cell contains both error strings drawn from its `ValidationResult.errors` list (joined into the single cell, typically newline-separated)
- **AND** the passing spec's `Errors` cell is empty

#### Scenario: --validate-only reports a missing spec.md as a directory-level failure
- **GIVEN** the project contains a non-skipped spec directory (e.g., `se3/specs/empty-dir/`) that has no `spec.md` file inside it
- **WHEN** the user runs `se3 sync --validate-only`
- **THEN** the Rich table includes a FAIL row for `empty-dir`
- **AND** that row's `Errors` cell contains exactly `"spec.md missing"`
- **AND** `validate_spec_structure` is NOT invoked for that directory
- **AND** the failure counts toward the exit status, so the command exits with status `1`

#### Scenario: --validate-only reports an unreadable spec.md as a directory-level failure
- **GIVEN** a non-skipped spec directory whose `spec.md` exists but raises `OSError` when read (e.g., permission denied)
- **WHEN** the user runs `se3 sync --validate-only`
- **THEN** the Rich table includes a FAIL row for that directory
- **AND** that row's `Errors` cell contains a single string beginning with `"read error: "` followed by the stringified exception
- **AND** `validate_spec_structure` is NOT invoked for that directory
- **AND** the failure counts toward the exit status, so the command exits with status `1`

#### Scenario: --validate-only skips underscore- and dot-prefixed spec directories
- **GIVEN** the project's `se3/specs/` tree contains a regular spec directory (e.g., `my-feature/`), an underscore-prefixed directory (e.g., `_scratch/`), and a dot-prefixed directory (e.g., `.archive/`)
- **WHEN** the user runs `se3 sync --validate-only`
- **THEN** only the regular spec directory is loaded and validated
- **AND** the underscore- and dot-prefixed directories are silently skipped
- **AND** any spec content inside the skipped directories does NOT affect the report or the exit status

#### Scenario: Way-A rollback falls back to direct write when git checkout fails
- **GIVEN** a Way-A sub-agent edit produces invalid content that fails `validate_spec_structure`
- **AND** the engine attempts `git checkout HEAD -- <spec-path>` to roll back, but the checkout fails (e.g., the spec is untracked or git is unavailable)
- **WHEN** the rollback path runs
- **THEN** the engine writes the pre-call disk content back to the spec file as a best-effort recovery
- **AND** the in-memory cache is updated to that pre-call content
- **AND** the engine returns failure for the edit without refreshing the cache from the invalid post-edit content

#### Scenario: Valid mid-flight Way-A edit is salvaged when LLM call raises
- **GIVEN** a sub-agent has already used the `Edit` tool to write a structurally valid spec body to disk
- **AND** the surrounding LLM call subsequently raises an exception before returning
- **WHEN** the engine compares pre-call and post-error disk snapshots and observes the spec file changed
- **AND** runs `validate_spec_structure` against the post-error content with `passed=True`
- **THEN** the engine accepts the partial edit as a successful Way-A update
- **AND** refreshes the in-memory cache from the new content
- **AND** returns success despite the LLM exception

#### Scenario: Invalid mid-flight Way-A edit is rolled back when LLM call raises
- **GIVEN** a sub-agent has used the `Edit` tool to write content to disk that fails `validate_spec_structure`
- **AND** the surrounding LLM call then raises an exception
- **WHEN** the engine detects the disk change and validation fails on the post-error content
- **THEN** the engine runs `git checkout HEAD -- <spec-path>` to roll the spec back
- **AND** refreshes the in-memory cache from the restored disk content (falling back to the captured pre-call content if the re-read fails)
- **AND** returns failure without caching the invalid post-edit content

#### Scenario: Mid-flight LLM error with no disk change returns failure without recovery
- **GIVEN** the LLM call raises an exception
- **AND** the pre-call and post-error disk snapshots of the spec file are identical (the sub-agent did not write to disk before the error)
- **WHEN** the engine inspects the snapshots
- **THEN** no validation or rollback is performed
- **AND** the engine returns failure for the spec update

### Requirement: Spec Body Extraction (Agentic Output Purification)

The spec-format package SHALL expose a pure helper `extract_spec_body(text: str, spec_name: str) -> str` that slices the markdown spec body out of an agentic sub-agent's raw output before that output is handed to `validate_spec_structure`. Sub-agent stdout in off-mode frequently carries narrative preamble (e.g., `"I have enough context…"`), tool-process chatter (`[tool_use] Read …`, `[tool_result] …`), and only then the actual spec document at its tail. Without purification, the leading prose would make the structural validator reject an otherwise-valid spec body, so the spec would never be written to disk. This helper is the single source of truth shared by `sync_discovery` (newly generated specs) and `sync_engine` Way B (full-rewrite updates).

**Behavior:**

1. The function MUST be a pure transform over `(text, spec_name)` — no filesystem reads, no network, no global state — and it MUST never raise.
2. It drops everything before the first structural anchor and returns the original string from that anchor onward, preserving the text verbatim from the anchor (including line endings) so no content is lost.
3. **Anchor precedence** (first match wins):
   1. The v1 marker line `<!-- spec-format: v1 -->`.
   2. A `# <spec_name> Specification` level-1 heading, matched with the same case-insensitive, dash-token-tolerant rule used by the Specification-title check of the structural contract.
   3. As a fallback, the first level-1 `# ` heading of any kind.
4. **No anchor found** — when none of the anchors is present (e.g., a pure meta-summary with no spec body), the function returns the text unchanged so the downstream `validate_spec_structure` gate can reject it on its own terms. The extractor is a purifier, not a gate: it never fabricates structure and never substitutes for validation.

**Caller obligations:**

- Callers SHALL invoke `extract_spec_body` AFTER stripping any outer markdown code fences and BEFORE calling `validate_spec_structure`.
- When a caller auto-prepends the v1 marker for an LLM that omitted it, the prepend MUST happen AFTER extraction so the marker attaches to the spec body and not to discarded narrative.

#### Scenario: Narrative preamble before the spec body is sliced off
- **GIVEN** a `text` consisting of a narrative preamble and tool-process lines followed by a complete spec body that begins with `<!-- spec-format: v1 -->`
- **WHEN** `extract_spec_body(text, name)` is called
- **THEN** the returned string begins at the v1 marker and contains none of the preceding narrative or tool-process text
- **AND** the subsequent `validate_spec_structure` call passes

#### Scenario: Body lacking the v1 marker is anchored on the Specification title
- **GIVEN** a `text` whose spec body lacks the v1 marker but contains a `# <spec_name> Specification` heading after some narrative
- **WHEN** `extract_spec_body(text, spec_name)` is called
- **THEN** the returned string begins at the `# <spec_name> Specification` heading
- **AND** a caller that auto-prepends the v1 marker afterward produces a body whose first line is the marker and whose second line is the Specification title

#### Scenario: Pure meta-summary with no anchor is returned unchanged for the validator to reject
- **GIVEN** a `text` that is entirely narrative prose with no v1 marker and no `# ` heading
- **WHEN** `extract_spec_body(text, name)` is called
- **THEN** the text is returned unchanged
- **AND** `validate_spec_structure` subsequently rejects it (narrative first line / missing structure) so nothing is written to disk

### Requirement: Orphan H2 Tracking

In addition to producing the list of Requirements, the spec parser SHALL detect and expose **orphan H2 headings** — second-level (`## `) headings that appear in the gap *between* Requirements rather than in the shared header at the top of the file. Orphan H2s represent a content-loss hazard because items-mode loading drops shared sections that appear after the first Requirement, so any prose attached to such headings would silently disappear from item-level output.

**Definition:**

- An **orphan H2** is a `## ` heading whose position in the file is strictly greater than the end of some Requirement's body and strictly less than the start of the next Requirement (or EOF if the orphan follows the last Requirement).
- A `## ` heading that appears before the first `### Requirement:` line is part of the shared header and is NOT an orphan.
- Each orphan H2 is identified by its full heading line text (e.g., `## Notes`) and a 1-based line number relative to the original file text.

**Parser contract:**

- `ParsedSpec` SHALL carry an `orphan_h2s` field whose value is a list of `(heading_text, line_number)` tuples, in document order.
- Duplicate orphan headings (identical heading line text) are deduplicated within the list: each distinct heading line appears at most once, retaining the first occurrence's line number.
- The list is empty (`[]`) when no orphan H2 is detected. The field MUST default to an empty list when a spec contains no orphan H2 headings.
- Orphan-H2 detection is purely informational at the parser layer: it does NOT cause `parse_spec()` to fail and does NOT alter the contents of `requirements`, `header_text`, or `trailing_text`.

**Relationship to other parser outputs:**

- `header_text` continues to capture only the text before the first Requirement; orphan H2 sections appearing between Requirements are NOT folded back into the shared header.
- `trailing_text` captures any non-empty text after the last Requirement's body; orphan H2 lines that fall in that trailing region also contribute entries to `orphan_h2s` so that downstream tooling can surface them.

#### Scenario: Orphan H2 between Requirements is detected
- **GIVEN** a spec containing `### Requirement: Foo`, followed by Foo's body, then `## Notes` with prose, then `### Requirement: Bar`
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.orphan_h2s` contains one entry whose heading text is `## Notes`
- **AND** the entry's line number matches the 1-based line where `## Notes` appears in the original file text

#### Scenario: Shared-header H2 is not treated as orphan
- **GIVEN** a spec with `## Purpose`, `## Definitions`, and then the first `### Requirement: Foo`
- **WHEN** `parse_spec()` is called
- **THEN** neither `## Purpose` nor `## Definitions` appears in `ParsedSpec.orphan_h2s`
- **AND** `ParsedSpec.orphan_h2s` equals `[]` if no other `## ` heading appears between Requirements

#### Scenario: Orphan H2 after the last Requirement is detected
- **GIVEN** a spec whose final `### Requirement: Foo` body is followed by a `## Appendix` heading and additional prose
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.orphan_h2s` includes an entry for `## Appendix` with its line number
- **AND** the prose beneath `## Appendix` is also captured in `ParsedSpec.trailing_text`

#### Scenario: Duplicate orphan H2 headings are deduplicated
- **GIVEN** a spec where the same heading line `## Notes` appears in two different inter-Requirement gaps
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.orphan_h2s` contains exactly one entry for `## Notes`
- **AND** that entry's line number corresponds to the first occurrence in document order

### Requirement: Trailing Text Preservation

In addition to the shared header and per-Requirement bodies, the spec parser SHALL capture any non-empty text that appears AFTER the last Requirement's body as **trailing text**. This region typically contains orphan H2 sections (e.g., a closing `## Appendix` or `## Notes` block at EOF) whose content would otherwise be lost when the loader emits a spec in items mode (since items-mode output is assembled from the shared header plus selected Requirement bodies and would naturally exclude anything after the last Requirement).

**Parser contract:**

- `ParsedSpec` SHALL carry a `trailing_text` field of type `str` whose value is the text that follows the last Requirement's body in the original file.
- The captured text is stripped of leading and trailing whitespace; if no non-whitespace text remains after the last Requirement, `trailing_text` defaults to the empty string `""`.
- When a spec has zero Requirements, `trailing_text` is `""` (there is no "last Requirement" boundary to capture text after; such content is part of the shared header instead).
- `trailing_text` capture is independent of `orphan_h2s`: an orphan H2 heading appearing after the last Requirement contributes both an `orphan_h2s` entry (for the heading itself) and is also included verbatim in `trailing_text` (heading plus body prose).

**Loader contract (items mode):**

- When the loader assembles a spec in items mode (i.e., emitting only selected Requirements rather than the full file), it SHALL append `parsed.trailing_text` to the spec's output after all selected Requirement bodies, separated by a blank line. This guarantees orphan EOF sections are not silently dropped when downstream consumers load a subset of items.
- If `trailing_text` is empty, no separator or trailing block is appended.
- The presence of a non-empty `trailing_text` is sufficient grounds to include the spec in items-mode output even when zero of the spec's Requirements matched the current selection: the loader SHALL emit the shared header plus the trailing text in that case, so that EOF content is never lost.

#### Scenario: Trailing text after last Requirement is captured
- **GIVEN** a spec whose final `### Requirement: Foo` body is followed (after the body ends) by a `## Appendix` heading and additional prose at EOF
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.trailing_text` is a non-empty string containing both the `## Appendix` heading line and the prose beneath it

#### Scenario: Empty trailing region yields empty string
- **GIVEN** a spec whose final Requirement body ends at EOF with no further non-whitespace content
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.trailing_text` equals `""`

#### Scenario: Items-mode loader preserves trailing text
- **GIVEN** a spec with a Requirement "Foo" followed by a `## Appendix` orphan section at EOF whose content is captured in `trailing_text`
- **WHEN** the loader assembles items-mode output that selects "Foo"
- **THEN** the emitted spec body includes Foo's heading and body
- **AND** the emitted body also includes the trailing text appended after Foo's body, separated by a blank line

#### Scenario: Items-mode loader emits spec for trailing text alone
- **GIVEN** a spec with a Requirement "Foo" followed by a non-empty trailing region
- **AND** the loader's current selection does not include "Foo" (zero matched Requirements for this spec)
- **WHEN** items-mode output is assembled
- **THEN** the spec is still included in the output with its shared header and the trailing text
- **AND** no Requirement bodies are emitted for this spec

### Requirement: Length-Shrink Warning Heuristic

In addition to the structural validation contract, `sync_engine` SHALL emit a non-fatal warning whenever a sub-agent edit produces new spec content that is substantially shorter than the prior on-disk content. This heuristic catches cases where a sub-agent inadvertently truncates a spec — for example, by replacing rich existing content with a brief summary — even when the truncated result still passes `validate_spec_structure`.

- The warning is emitted by BOTH the Way A (in-place `Edit`) and Way B (full-rewrite markdown) write paths. The length comparison is performed BEFORE `validate_spec_structure` runs on the candidate content, so the warning may fire for a write that is subsequently rejected by structural validation and rolled back. The warning is purely a length-heuristic signal and does not depend on validation outcome.
- The trigger is `len(new_content) < 0.5 * len(original_content)` — i.e., the new content is less than 50% of the size of the prior content.
- The warning is informational only: it does NOT cause the engine to reject the edit, roll back the file, or skip the in-memory cache refresh. When the candidate subsequently passes structural validation, the new content is persisted and the cache is refreshed; when it fails validation, the normal rollback path runs and the shrink warning remains in the operator-visible log as a record of the rejected candidate.
- The length comparison is independent of the structural validator and is NOT part of the `validate_spec_structure` contract. Callers of the validator itself (e.g., `sync_discovery`, `se3 sync --validate-only`) MUST NOT apply this heuristic.
- The warning is intended for operator visibility (round reports, logs) so that suspicious shrinkage can be reviewed by a human, even though the spec remains structurally valid.

#### Scenario: Way-A edit that halves spec content emits a shrink warning
- **GIVEN** a spec file whose existing content is 4000 characters
- **AND** a sub-agent Way-A edit produces a new on-disk body of 1500 characters that still passes `validate_spec_structure`
- **WHEN** the engine completes the Way-A path
- **THEN** the engine emits a length-shrink warning naming the spec
- **AND** the new content is persisted to disk
- **AND** the in-memory cache is refreshed to the new content

#### Scenario: Way-B rewrite within the shrink threshold emits no warning
- **GIVEN** a spec file whose existing content is 4000 characters
- **AND** a sub-agent Way-B rewrite produces a new body of 3000 characters that passes `validate_spec_structure`
- **WHEN** the engine writes the Way-B result
- **THEN** no length-shrink warning is emitted because the new size is ≥ 50% of the original

#### Scenario: Shrink warning does not block persistence
- **GIVEN** a Way-A or Way-B edit that triggers the length-shrink warning
- **WHEN** the engine processes the result
- **THEN** the warning is recorded for operator visibility
- **AND** the engine still writes the new content and refreshes the in-memory cache
- **AND** the engine returns success (the shrink heuristic is non-fatal)

#### Scenario: Shrink warning fires before structural validation rejects the candidate
- **GIVEN** a spec file whose existing content is 4000 characters
- **AND** a sub-agent Way-A or Way-B edit produces a candidate body of 1500 characters that subsequently FAILS `validate_spec_structure`
- **WHEN** the engine processes the result
- **THEN** the length-shrink warning is emitted (because the length check runs before structural validation)
- **AND** the engine still rolls back the invalid candidate via the normal validation-failure path
- **AND** the in-memory cache is NOT refreshed from the rejected content

### Requirement: Heading Nesting Limit

Format v1 caps Markdown heading nesting at five hash marks (`#####`). The five-hash level is reserved for Scenario sub-headings under a Requirement (e.g. `##### Scenario: ...`). Any heading with **six or more** leading hash marks (`######`, `#######`, …) is rejected as exceeding the v1 nesting range, because deeper nesting has no defined semantics in this spec format and would obscure the Requirement / Scenario boundary that downstream tooling relies on.

**Parser contract:**

- `ParsedSpec` SHALL carry a `deep_heading_lines` field whose value is a list of 1-based line numbers (`List[int]`) identifying every heading in the original file text whose marker matches `^######+\s+` — i.e., six or more hash marks followed by whitespace.
- Headings appearing inside fenced code blocks (delimited by triple-backtick fences) MUST NOT be reported as deep headings; the parser strips code-block ranges before testing for deep-heading matches.
- The list is in document order (ascending line numbers) and defaults to an empty list (`[]`) when no deep headings are present.
- Detection of deep headings is purely informational at the parser layer: it does NOT cause `parse_spec()` to fail and does NOT alter the contents of `requirements`, `header_text`, `trailing_text`, or `orphan_h2s`.

**Validator contract:**

- The `validate(parsed: ParsedSpec) -> List[Issue]` function SHALL emit an `Issue` with `severity="error"` whenever `parsed.deep_heading_lines` is non-empty.
- The error's `message` SHALL state that heading nesting exceeds the v1 allowed range (e.g. "Heading nesting exceeds v1 allowed range (###### or deeper is not permitted)").
- The error's `location` SHALL name the first offending line (e.g. `"line 42"`), using the first entry of `deep_heading_lines`. Multiple deep headings in a single spec SHALL produce a single error issue keyed off the first offender, not one issue per heading.
- This deep-heading check is part of the `validate()` issue stream and is independent of `validate_spec_structure`; the structural-validation contract above is unchanged by this rule.

#### Scenario: Deep heading line numbers are exposed by the parser
- **GIVEN** a spec file that contains a `###### Sub-detail` heading on line 50 outside of any code block
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.deep_heading_lines` contains the integer `50`
- **AND** the deep heading does not cause `parse_spec()` to raise or alter `requirements`

#### Scenario: Deep heading inside a fenced code block is ignored
- **GIVEN** a spec file whose only `###### ...` line lies inside a fenced triple-backtick code block (e.g. inside a Markdown example)
- **WHEN** `parse_spec()` is called
- **THEN** `ParsedSpec.deep_heading_lines` equals `[]`
- **AND** the validator does NOT emit a deep-heading error for that fenced occurrence

#### Scenario: Validator emits an error for deep headings
- **GIVEN** a parsed spec whose `deep_heading_lines` is `[42, 88]`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issue list includes exactly one `Issue` with `severity="error"`, a message naming the v1 nesting range violation, and `location="line 42"`
- **AND** no additional issue is emitted for line 88 (a single error keyed off the first offender suffices)

#### Scenario: Five-hash Scenario heading is not flagged
- **GIVEN** a spec containing a Requirement followed by `##### Scenario: Happy path`
- **WHEN** `parse_spec()` is called and `validate(parsed)` is invoked
- **THEN** `ParsedSpec.deep_heading_lines` equals `[]`
- **AND** no deep-heading error is emitted (because five hash marks is the maximum allowed level, reserved for Scenarios)

### Requirement: Parsed-Spec Issue Validator

In addition to the structural `validate_spec_structure(content, spec_name)` entry point — which operates on a raw candidate buffer and short-circuits to a single PASS/FAIL decision used by write-back gates — the spec-format package SHALL expose a second, complementary validator that operates on a `ParsedSpec` and produces a stream of `Issue` records describing every rule the spec violates. This validator is meant for diagnostic surfaces (audits, IDE-style linting, round reports) where callers want the full list of problems rather than a single boolean.

**Signature and result contract:**

- The function SHALL be exposed as `validate(parsed: ParsedSpec) -> List[Issue]`.
- Each `Issue` SHALL carry three fields:
  - `severity`: one of the strings `"error"` or `"warning"`.
  - `message`: a human-readable description of the violation, suitable for direct display.
  - `location`: a string locating the offender — `"header"` for spec-level issues, `"line <N>"` for line-anchored issues (using 1-based line numbers from the parsed source), or `"Requirement: <name>"` for issues attached to a specific Requirement.
- The function MUST be pure over its `ParsedSpec` input (no filesystem, network, LLM, or global state) and MUST NOT raise on any well-formed `ParsedSpec`; well-formed inputs that violate format v1 rules yield issues, not exceptions.
- The returned list is in the order that checks are applied (see below); a spec with zero violations yields the empty list `[]`.
- `validate()` is independent of `validate_spec_structure()`: the two functions share rule intent for some checks (e.g. v1 marker presence, `## Purpose` presence) but differ in severity, output shape, and intended use. `validate_spec_structure()` is the write-back gate; `validate()` is the diagnostic enumerator. Callers SHOULD NOT substitute one for the other.

**Checks performed (each produces zero or more `Issue` records):**

The validator inspects the parsed spec in the following order. All applicable issues are appended; one check failing does NOT short-circuit subsequent checks.

1. **Missing v1 marker** (`severity="warning"`) — If `parsed.has_v1_marker` is false, append one issue with `location="header"` and a message naming the missing format version declaration.
2. **Missing `## Purpose` section** (`severity="warning"`) — If `parsed.header_text` does not contain a `## Purpose` heading (case-insensitive substring match against `## purpose` is sufficient), append one issue with `location="header"`. This is a warning (not an error) because the Purpose section is a strong convention but the parser does not strictly require it for downstream consumption; the stricter structural gate in `validate_spec_structure` enforces it as a hard rejection for write-back.
3. **Orphan H2 headings** (`severity="warning"`, one issue per orphan) — For every entry in `parsed.orphan_h2s`, append one issue with `location="line <N>"` and a message naming the offending heading text plus the content-loss hazard (e.g., "content under this heading is not attached to any Requirement and is lost in items mode"). Each distinct orphan H2 yields a separate issue (one per heading), not a single aggregated entry.
4. **Deep headings** (`severity="error"`, one issue total) — If `parsed.deep_heading_lines` is non-empty, append exactly one issue with `location="line <N>"` where `<N>` is the first entry of the list, and a message naming the v1 nesting-range violation. This intentionally matches the rule defined in [[Heading Nesting Limit]]: a single error keyed off the first offender, never one issue per deep heading.
5. **Requirement name checks** (multiple `severity="error"` issues possible) — Iterate `parsed.requirements` in document order. For each Requirement:
   - **Empty name** — If the Requirement's `name` is empty or whitespace-only (i.e., the file contained a literal `### Requirement:` heading with no name token), append an issue with `severity="error"`, `location="line <N>"` (using the Requirement's `line_start`, or `"line ?"` if line tracking is unavailable), and a message naming the empty-name condition. After emitting this issue the iteration continues with the next Requirement; the empty-name Requirement is NOT subjected to the illegal-character or duplicate-name checks below (avoiding a cascade of redundant errors on a single broken heading).
   - **Illegal characters in name** — If the name contains any ASCII control character (the byte ranges `0x00`–`0x1F` or `0x7F`), append an issue with `severity="error"`, `location="Requirement: <name>"`, and a message naming the illegal characters (the name is included in `repr()` form so non-printable bytes are visible). Tab, newline, and other control codes are rejected; ordinary printable characters (letters, digits, spaces, hyphens, punctuation outside the control range) are accepted at this layer.
   - **Duplicate name** — Track names already seen during the iteration. If the current name has already been recorded, append an issue with `severity="error"`, `location="Requirement: <name>"`, and a message naming the duplicate. Otherwise, record the name in the seen-set. Duplicate detection is by exact case-sensitive string equality of the name token.

**Relationship to other validators and callers:**

- `validate()` is the canonical source of `Issue` records for tooling that wants the full diagnostic picture; it complements but does NOT replace the structural-validation gate enforced by `validate_spec_structure`. Write-back paths SHALL continue to use `validate_spec_structure` for accept/reject decisions; `validate()` is informational.
- The [[Heading Nesting Limit]] Requirement describes the underlying `deep_heading_lines` detection and the validator's single-error contract for that rule; this Requirement governs the broader `validate()` surface that emits that error alongside the other warnings and errors enumerated above.
- The validator MUST tolerate a `ParsedSpec` produced from a spec in lenient mode (no v1 marker). The missing-marker check is itself one of the warnings the validator emits, so passing such a spec to `validate()` SHALL yield a warning issue rather than a failure to evaluate.

#### Scenario: Clean parsed spec yields no issues
- **GIVEN** a `ParsedSpec` with `has_v1_marker=True`, a `## Purpose` section in `header_text`, no orphan H2s, no deep headings, and Requirements whose names are unique and contain no control characters
- **WHEN** `validate(parsed)` is called
- **THEN** the returned list equals `[]`

#### Scenario: Missing v1 marker emits a warning
- **GIVEN** a `ParsedSpec` whose `has_v1_marker` is `False`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include exactly one entry with `severity="warning"`, `location="header"`, and a message naming the missing v1 format marker

#### Scenario: Missing Purpose section emits a warning
- **GIVEN** a `ParsedSpec` whose `header_text` does not contain a `## Purpose` heading (case-insensitive)
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include exactly one entry with `severity="warning"`, `location="header"`, and a message naming the missing Purpose section

#### Scenario: Each orphan H2 emits its own warning
- **GIVEN** a `ParsedSpec` whose `orphan_h2s` lists `("## Notes", 80)` and `("## Appendix", 120)`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include two `severity="warning"` entries, one with `location="line 80"` naming `## Notes` and another with `location="line 120"` naming `## Appendix`
- **AND** both messages reference the items-mode content-loss hazard

#### Scenario: Deep headings emit a single error keyed off the first offender
- **GIVEN** a `ParsedSpec` whose `deep_heading_lines` is `[42, 88]`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include exactly one `severity="error"` entry with `location="line 42"` and a message naming the v1 nesting-range violation
- **AND** no additional error is emitted for line 88

#### Scenario: Duplicate Requirement names emit an error on the second occurrence
- **GIVEN** a `ParsedSpec` whose `requirements` contains two Requirements both named `"Foo"`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include one `severity="error"` entry with `location="Requirement: Foo"` and a message naming the duplicate name
- **AND** the first occurrence of `"Foo"` does NOT itself produce a duplicate-name error (only the second occurrence does)

#### Scenario: Requirement name with control character emits an illegal-characters error
- **GIVEN** a `ParsedSpec` whose `requirements` contains a Requirement whose `name` includes an ASCII control character (any byte in `0x00`–`0x1F` or `0x7F`, e.g. `\x07` or a literal tab)
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include one `severity="error"` entry with `location="Requirement: <name>"` and a message naming the illegal characters, with the offending name included in `repr()` form so the control byte is visible

#### Scenario: Empty Requirement name emits a single error and skips further name checks
- **GIVEN** a `ParsedSpec` whose `requirements` contains a Requirement whose `name` is empty or whitespace-only (the source file contained `### Requirement:` with no name token), with `line_start=17`
- **WHEN** `validate(parsed)` is called
- **THEN** the returned issues include one `severity="error"` entry with `location="line 17"` and a message naming the empty-name condition
- **AND** that same Requirement does NOT additionally produce an illegal-character error or a duplicate-name error in this iteration step
