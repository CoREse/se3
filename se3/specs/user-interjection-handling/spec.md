<!-- spec-format: v1 -->
# user-interjection-handling Specification

## Purpose

The `user-interjection-handling` subsystem provides the single, central composition primitive used to inline user interjections — additional instructions typed mid-flow after a Ctrl-C interrupt — into a step's effective `task_description`. Interjections are persisted into `flow.state.context["user_interjections"]` by the interrupt handler, then folded into the effective `task_description` for both (a) the current step's immediate re-run (via `run.py:_handle_step_interrupt`) and (b) every subsequent step's input build (via `state_machine._build_step_inputs`). The module owns the exact rendered format of the appended `## Additional Instructions (added during run)` section so that both call sites produce byte-identical output and so that re-composition against the same base does not produce nested or doubled sections.

## Requirements

### Requirement: Composer entry point

The module exposes a single public function `compose_task_description_with_interjections(base, interjections)` that takes the canonical base task description and an iterable of interjection mappings and returns a string. It is the sole renderer of the appended interjection section; both `run.py` and `state_machine` MUST route through it to guarantee identical output.

#### Scenario: Returns string output
- **WHEN** the function is invoked with any combination of `base` and `interjections`
- **THEN** the return value is a `str`
- **AND** no exception is raised for empty, missing, or malformed entries

### Requirement: Empty / no-op cases preserve base

When there is nothing meaningful to render, the function returns the base verbatim (or empty), and never emits a bare section header.

#### Scenario: Empty interjections iterable returns base unchanged
- **WHEN** `interjections` is an empty iterable
- **THEN** the result equals `base` unchanged

#### Scenario: Empty base and empty interjections returns empty string
- **WHEN** both `base` is `""` and `interjections` is empty
- **THEN** the result is `""`

#### Scenario: `None` base coerces to empty
- **WHEN** `base` is falsy (e.g. `None` or `""`) and `interjections` is empty
- **THEN** the result is `""` (never `None`)

#### Scenario: Only-unusable entries fall back to base
- **WHEN** every entry in `interjections` is skipped (not a Mapping, missing `text`, or `text` is whitespace-only)
- **THEN** the result equals `base or ""` and the `## Additional Instructions` header is NOT emitted

### Requirement: Entry shape and validation

Each interjection entry is a `Mapping` with the optional keys `text`, `step_type`, and `timestamp`. The composer is defensive: non-Mapping entries are silently skipped, and only entries with non-whitespace `text` are rendered.

#### Scenario: Non-Mapping entries are skipped
- **WHEN** `interjections` contains a non-Mapping element (e.g. a string, `None`, a list)
- **THEN** that element is ignored
- **AND** remaining valid entries are still rendered

#### Scenario: Entry with missing or empty text is skipped
- **WHEN** an entry's `text` is missing, `None`, `""`, or only whitespace
- **THEN** that entry contributes nothing to the output
- **AND** it does not produce an empty bullet

#### Scenario: Text is stripped of surrounding whitespace
- **WHEN** an entry's `text` has leading or trailing whitespace
- **THEN** the rendered bullet uses the `.strip()`ped text

### Requirement: Bullet rendering and prefix format

Each retained entry becomes a Markdown bullet line `- {prefix}{text}`. The prefix encodes `step_type` and/or `timestamp` in a `[...]` form so that downstream readers can attribute when each interjection was injected.

#### Scenario: Both step_type and timestamp present
- **WHEN** an entry has non-empty `step_type` and `timestamp`
- **THEN** the bullet is rendered as `- [{step_type}@{timestamp}] {text}`

#### Scenario: Only step_type present
- **WHEN** an entry has non-empty `step_type` and a missing/empty `timestamp`
- **THEN** the bullet is rendered as `- [{step_type}] {text}`

#### Scenario: Only timestamp present
- **WHEN** an entry has a non-empty `timestamp` and a missing/empty `step_type`
- **THEN** the bullet is rendered as `- [{timestamp}] {text}`

#### Scenario: Neither step_type nor timestamp present
- **WHEN** both `step_type` and `timestamp` are missing or empty
- **THEN** the bullet is rendered as `- {text}` with no bracketed prefix

#### Scenario: Step / timestamp values are stripped
- **WHEN** `step_type` or `timestamp` have surrounding whitespace
- **THEN** the values used in the prefix are the `.strip()`ped values
- **AND** a `step_type` or `timestamp` consisting only of whitespace is treated as absent

### Requirement: Section header and layout

When at least one entry is renderable, the composer appends a single `## Additional Instructions (added during run)` Markdown section, separated from the base by a blank line, with one bullet per entry in input order.

#### Scenario: Non-empty base with renderable entries
- **WHEN** `base` is non-empty (after `rstrip`) and at least one entry is renderable
- **THEN** the output is exactly `{base.rstrip()}\n\n## Additional Instructions (added during run)\n\n{bullets joined by newline}`

#### Scenario: Empty / whitespace-only base with renderable entries
- **WHEN** `base` is empty or contains only trailing whitespace (such that `base.rstrip()` is `""`)
- **THEN** the output starts with `## Additional Instructions (added during run)\n\n` followed by the bullets, with no leading newlines

#### Scenario: Trailing whitespace on base is trimmed before joining
- **WHEN** `base` ends with newlines or spaces
- **THEN** the base portion of the output is `base.rstrip()` (no extra blank lines between base and the section header beyond the single `\n\n` separator)

#### Scenario: Bullet ordering preserved
- **WHEN** multiple renderable entries are supplied
- **THEN** their bullets appear in the order they were yielded by the input iterable, joined by `\n`

#### Scenario: Exactly one section header per composition
- **WHEN** the composer is invoked with any non-empty set of renderable interjections
- **THEN** the output contains the literal `## Additional Instructions` exactly once

### Requirement: Deterministic / re-composable output

Because both the re-run path (`run.py:_handle_step_interrupt`) and the propagation path (`state_machine._build_step_inputs`) recompose against the original base task description, calling the composer repeatedly against the same `base` with a growing `interjections` list MUST yield a single section — never a nested or doubled one. The function is pure: it does not mutate `base` or `interjections`, performs no I/O, and depends only on its arguments.

#### Scenario: Repeated composition against original base does not nest sections
- **WHEN** the composer is called with the canonical original `base` and an `interjections` list that has grown (e.g., after a second Ctrl-C interjection appended)
- **THEN** the output contains exactly one `## Additional Instructions` header
- **AND** every interjection's text appears exactly once in input order

#### Scenario: Byte-identical output across call sites
- **WHEN** `run.py` and `state_machine._build_step_inputs` invoke the composer with the same `base` and same `interjections` sequence
- **THEN** both produce byte-identical strings (the module is the sole owner of the rendered format)

#### Scenario: Pure function — no mutation of inputs
- **WHEN** the composer is invoked
- **THEN** `base` is not modified, the `interjections` iterable's underlying entries are not mutated, and no global state is touched

### Requirement: Iterable input contract

The `interjections` parameter is typed as `Iterable[Mapping[str, Any]]`. The composer iterates it exactly once and tolerates `None` in place of an iterable.

#### Scenario: `None` interjections treated as empty
- **WHEN** `interjections` is `None`
- **THEN** the function behaves as if it were an empty iterable (no exception, returns `base or ""`)

#### Scenario: Generators are acceptable
- **WHEN** `interjections` is a single-use iterable (generator)
- **THEN** the composer iterates it exactly once and produces the correct output